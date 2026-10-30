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
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.display_time import DisplayTimeJSONResponse
from app.core.errors import AppError, envelope
from app.models.ledger import User
from app.services import login_guard
from app.services import users as users_service

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger("launchpad.auth")

COOKIE_NAME = "launchpad_session"
LAB_COOKIE_NAME = "launchpad_lab_session"
# Default absolute session lifetime; the effective value is
# `Settings.auth_session_ttl_seconds` (r12-g) — read it through `session_ttl_seconds()`.
SESSION_TTL_SECONDS = 12 * 3600
# v2 bound a registered account's session to its IMMUTABLE ``users.id`` (v1 carried
# the username only, so a deleted-and-re-registered username kept an old cookie
# valid for the NEW account). v3 (r12-g) adds the session epoch: a registered
# account's ``users.session_epoch`` (bumped on every password change / reset), or a
# digest of the configured password for the built-in admin — so a new password signs
# the account's other sessions out at once. Older cookies are refused (one re-login).
_COOKIE_VERSION = "3"

_OPEN_API_PATHS = {
    "/api/auth/login",
    "/api/auth/status",
    "/api/auth/register",
    "/api/health",
}

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

# Member-grantable agent-management capabilities (route_policy `perm:` values).
# A member holds every key by default; `users.permissions` stores only explicit
# denials. Admins (and the config-driven built-in admin) implicitly hold all.
AGENT_PERMISSIONS = users_service.AGENT_PERMISSIONS


def granted_permissions(overrides: dict[str, bool] | None) -> frozenset[str]:
    """The granted keys for a member row's `permissions` column value."""
    return frozenset(
        key for key in AGENT_PERMISSIONS if (overrides or {}).get(key) is not False
    )


@dataclass(frozen=True)
class Identity:
    """The resolved caller behind a valid session cookie."""

    username: str
    role: str
    email: str | None = None
    account_expires_at: datetime | None = None
    user_id: str | None = None  # None for the config-driven admin
    permissions: frozenset[str] = frozenset(AGENT_PERMISSIONS)
    session_epoch: int = 0  # registered accounts: users.session_epoch at issue time

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    def can(self, permission: str) -> bool:
        return self.is_admin or permission in self.permissions


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
    plain HTTP, and a `Secure` cookie there is silently never sent back. The explicit
    service-wide HTTP lab profile also permits HTTP without changing run_mode.
    """
    current = settings or get_settings()
    if current.console_http_lab:
        return False
    return current.auth_cookie_secure or current.run_mode == "prod"


def cookie_name(settings: Settings | None = None) -> str:
    """One service-wide cookie for either transport; a new name avoids Secure-cookie
    overwrite rejection when an existing HTTPS installation adds its HTTP lab entry."""
    current = settings or get_settings()
    return LAB_COOKIE_NAME if current.console_http_lab else COOKIE_NAME


def session_ttl_seconds(settings: Settings | None = None) -> int:
    """Absolute lifetime of a freshly issued session cookie (not an idle timeout: the
    signed expiry is never refreshed on activity — see `docs/cn/interfaces/
    users-and-ownership.md` §8)."""
    return int((settings or get_settings()).auth_session_ttl_seconds)


def registration_requires_approval(settings: Settings | None = None) -> bool:
    return (settings or get_settings()).auth_registration_require_approval


def registration_enabled(settings: Settings | None = None) -> bool:
    current = settings or get_settings()
    # With the gate disabled the console is already open, so there is nothing to
    # register an account for.
    return enabled(current) and current.auth_registration_enabled


_SECRET_LOCK = threading.Lock()
_SECRET_CACHE: dict[str, str] = {}


def _generate_session_secret_file(path: Path) -> str:
    """Create `path` 0600 with a fresh 32-byte hex secret; return the winner's content.

    `O_EXCL` makes two processes starting at once agree on one secret: the loser
    reads what the winner wrote.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(candidate + "\n")
    logger.warning("session secret generated at %s (every existing session is signed out)", path)
    return candidate


def session_secret(settings: Settings | None = None) -> str:
    """The cookie-signing secret (r12-g, A-3).

    `LAUNCHPAD_SESSION_SECRET` wins when set; otherwise the secret lives in
    `session_secret_file` (default `data/session_secret`, 0600), generated on first
    start. Cached per path so the hot path never touches the disk; rotating = set a
    new env value, or delete the file and restart. The secret is deliberately NOT
    derived from the admin password any more: a password rotation keeps sessions,
    and a leaked password no longer lets anyone forge a cookie offline.
    """
    current = settings or get_settings()
    if current.session_secret is not None and current.session_secret.get_secret_value():
        return current.session_secret.get_secret_value()
    path_text = current.session_secret_file
    with _SECRET_LOCK:
        cached = _SECRET_CACHE.get(path_text)
        if cached:
            return cached
        path = Path(path_text)
        value = path.read_text(encoding="utf-8").strip() if path.exists() else ""
        if not value:
            if path.exists():
                path.unlink()  # empty / truncated file: regenerate rather than sign with ""
            value = _generate_session_secret_file(path)
        _SECRET_CACHE[path_text] = value
        return value


def reset_session_secret_cache() -> None:
    """Forget the cached secret so the next cookie re-reads env / file (tests, rotation)."""
    with _SECRET_LOCK:
        _SECRET_CACHE.clear()


def _signing_key(settings: Settings | None = None) -> bytes:
    material = f"agentcore-launchpad-session:{session_secret(settings)}"
    return hashlib.sha256(material.encode("utf-8")).digest()


def admin_session_epoch(settings: Settings | None = None) -> str:
    """The built-in admin's epoch: a digest of its configured password, so rotating the
    password invalidates its sessions even though it has no `users` row to bump."""
    material = f"agentcore-launchpad-admin-epoch:{_password(settings) or ''}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _encode_payload(
    subject: str, expiry: int, user_id: str | None = None, epoch: str = ""
) -> str:
    # ``user_id`` is empty for the row-less config admin — its own stable principal.
    raw = f"{_COOKIE_VERSION}:{subject}:{user_id or ''}:{epoch}:{expiry}".encode()
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
    epoch: str | None = None,
) -> str:
    # Default epoch = the one a freshly issued cookie would carry today, so tests that
    # mint a cookie by hand keep working while a password rotation still ends it.
    if epoch is None:
        epoch = admin_session_epoch(settings) if user_id is None else "0"
    return _sign(_encode_payload(subject, expiry, user_id, epoch), settings)


def _decode(
    cookie: str | None, settings: Settings | None = None
) -> tuple[str, str | None, str, int] | None:
    """`(subject, user_id | None, epoch, expiry)` for an authentic, unexpired v3 cookie."""
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
    head, _, expiry_text = rest.rpartition(":")
    head, _, epoch = head.rpartition(":")
    subject, _, user_id = head.rpartition(":")
    if version != _COOKIE_VERSION or not subject or not expiry_text.isdigit():
        return None
    expiry = int(expiry_text)
    return (subject, user_id or None, epoch, expiry) if expiry > time.time() else None


def resolve_identity(
    request: Request,
    settings: Settings | None = None,
    db: Session | None = None,
) -> Identity | None:
    """Resolve the caller, or None when the session must be rejected.

    Rejected: missing/tampered/expired cookie, unknown subject, disabled account,
    expired account. The config admin short-circuits before any DB access, so the
    single-operator deployment stays database-free on the hot path.
    """
    current = settings or get_settings()
    decoded = _decode(request.cookies.get(cookie_name(current)), current)
    if decoded is None:
        return None
    subject, user_id, epoch, _ = decoded
    if user_id is None and hmac.compare_digest(
        subject.encode("utf-8"), current.auth_username.encode("utf-8")
    ):
        # the admin's epoch is its password digest: a rotation ends every admin session
        if not hmac.compare_digest(epoch.encode("ascii"), admin_session_epoch(current).encode()):
            return None
        return Identity(username=current.auth_username, role=ROLE_ADMIN)
    if user_id is None:
        return None  # a registered account's session always names its user id

    owned = db is None
    session = db or SessionLocal()
    try:
        # Resolved by the immutable id, then the display name must still match: a
        # deleted account's cookie names an id that no longer exists, and never the
        # new account that re-registered the same username.
        user = session.get(User, user_id)
        if (
            user is None
            or user.username_key != users_service.normalize_username(subject).lower()
            or user.status != "active"
            or users_service.is_expired(user)
            # issued before the account's last password change / reset
            or epoch != str(user.session_epoch or 0)
        ):
            return None
        return Identity(
            username=user.username,
            role=user.role,
            email=user.email,
            account_expires_at=users_service.as_utc(user.expires_at),
            user_id=user.id,
            permissions=granted_permissions(user.permissions),
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
    return (path == "/api" or path.startswith("/api/")) and path not in _OPEN_API_PATHS


async def auth_middleware(request: Request, call_next: Any) -> Any:
    """Require a live console session while leaving health and /v1 intact.

    Two guards, in order: an unauthenticated console may only be reached from
    loopback (T1), and an authenticated one needs a live session. The first is
    checked per request rather than at startup because that is the only place the
    real peer is known — `create_app()` cannot see uvicorn's `--host`, so a
    startup-only check is bypassable by launching uvicorn directly.
    """
    settings = get_settings()
    if request.method != "OPTIONS":
        path = app_path(request)
        if _is_guarded_api_path(path):
            if not enabled(settings):
                if not settings.allow_open_console and not _peer_is_loopback(request):
                    return DisplayTimeJSONResponse(
                        status_code=403,
                        content=envelope("auth.open_console_refused", OPEN_CONSOLE_REMEDY),
                    )
            else:
                identity = resolve_identity(request, settings)
                if identity is None:
                    return DisplayTimeJSONResponse(
                        status_code=401,
                        content=envelope("auth.required", "Authentication required"),
                    )
                # Route-policy dependencies run after this middleware and need
                # the same live identity. Reuse it instead of opening a second
                # ledger session for every authenticated request.
                request.state.identity = identity
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


def app_path(request: Request) -> str:
    """The request path as the app's routes see it — `request.url.path` minus the ASGI
    `root_path` (N17-obs284 rev-r26 N1). Starlette routes on the stripped path, while
    `url.path` keeps the prefix, so a prefix test on the raw `url.path` would skip every
    `/api` guard once a `root_path` is configured. `create_app()` sets none today (pinned by
    `tests/test_route_policy.py`); this keeps both guards right if one is ever set."""
    path = request.url.path
    root = (request.scope.get("root_path") or "").rstrip("/")
    if root and (path == root or path.startswith(f"{root}/")):
        path = path[len(root):] or "/"
    return path


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
    """Admin always passes; a member needs the (default-granted) permission."""
    identity = require_identity(request)
    if not identity.can(permission):
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
            "user_id": None,
            "role": None,
            "email": None,
            "account_expires_at": None,
            "permissions": [],
        }
    return {
        "username": identity.username,
        # the ownership key (`users.id`) the console compares `created_by` against to
        # offer creator-only actions (r13-g); None for the row-less config admin
        "user_id": identity.user_id,
        "role": identity.role,
        "email": identity.email,
        "account_expires_at": (
            identity.account_expires_at.isoformat()
            if identity.account_expires_at
            else None
        ),
        "permissions": sorted(identity.permissions),
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
    # Nothing about the configured identity is disclosed before authentication.
    return {
        "auth_required": required,
        "authenticated": identity is not None,
        "registration_enabled": registration_enabled(settings),
        "registration_requires_approval": registration_requires_approval(settings),
        # additive (N14-obs3): what the registration form hints / pre-validates from
        "password_policy": users_service.password_policy(settings),
        **_identity_fields(identity if required else None),
    }


def _authenticate(req: LoginRequest, settings: Settings, password: str) -> Identity:
    """The identity behind valid credentials; raises the 401 `auth.*` codes otherwise.

    Unknown account and wrong password share one code and one message
    (`auth.invalid_credentials`), and `services.users.authenticate` burns the same
    PBKDF2 work on an unknown name, so neither the text nor the timing discloses
    whether an account exists.
    """
    username_ok = hmac.compare_digest(
        req.username.strip().encode("utf-8"),
        settings.auth_username.encode("utf-8"),
    )
    password_ok = hmac.compare_digest(
        req.password.encode("utf-8"),
        password.encode("utf-8"),
    )
    if username_ok and password_ok:
        return Identity(username=settings.auth_username, role=ROLE_ADMIN)
    if username_ok:
        # the built-in admin never falls through to the ledger
        raise AppError(
            "auth.invalid_credentials", "Invalid username or password", status_code=401
        )
    db = SessionLocal()
    try:
        user = users_service.authenticate(db, req.username, req.password)
        users_service.record_login(db, user)
        return Identity(
            username=user.username,
            role=user.role,
            email=user.email,
            account_expires_at=users_service.as_utc(user.expires_at),
            user_id=user.id,
            permissions=granted_permissions(user.permissions),
            session_epoch=user.session_epoch or 0,
        )
    finally:
        db.close()


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
            "password_policy": users_service.password_policy(settings),
            **_identity_fields(None),
        }

    # Login protection (r12-g, N12-obs57). The per-name lock is checked BEFORE any
    # credential work, keyed by the submitted name whether or not it names an account.
    # The built-in config admin is EXEMPT from the lock (rev-r11 ruling): it is the
    # break-glass credential with the strong configured password, and a per-name lock
    # would let anyone on the internet keep the operator out; the peer window still
    # covers it. Only a wrong password counts as a failure — a correct password on a
    # pending / disabled / expired account proved ownership. The per-peer window is
    # consulted only AFTER a failed check: behind nginx (or the NLB) every caller
    # shares one peer address, so a tripped window turns further FAILURES into 429 but
    # never refuses a correct password.
    account_key = users_service.normalize_username(req.username).lower()
    lockable = account_key != settings.auth_username.strip().lower()
    peer = login_guard.peer_address(request)
    if lockable:
        login_guard.guard.check(account_key, peer, settings)
    try:
        identity = _authenticate(req, settings, password)
    except AppError as exc:
        if exc.code == "auth.invalid_credentials":
            waiting = login_guard.guard.failed(
                account_key, peer, settings, count_account=lockable
            )
            if waiting:
                raise login_guard.rate_limited_error(waiting) from None
        raise
    if lockable:
        login_guard.guard.record_success(account_key)

    expiry = int(time.time()) + session_ttl_seconds(settings)
    if identity.account_expires_at is not None:
        # never outlive the account itself
        expiry = min(expiry, int(identity.account_expires_at.timestamp()))
    max_age = max(1, expiry - int(time.time()))
    response.set_cookie(
        cookie_name(settings),
        _issue(
            identity.username,
            expiry,
            settings,
            user_id=identity.user_id,
            epoch=(
                admin_session_epoch(settings)
                if identity.user_id is None
                else str(identity.session_epoch)
            ),
        ),
        max_age=max_age,
        httponly=True,
        secure=cookie_secure(settings),
        samesite="lax",
        path="/",
    )
    return {
        "ok": True,
        "auth_required": True,
        "expires_at": expiry,
        "registration_enabled": registration_enabled(settings),
        "registration_requires_approval": registration_requires_approval(settings),
        "password_policy": users_service.password_policy(settings),
        **_identity_fields(identity),
    }


def _pending_shaped(username: str, email: str, settings: Settings) -> dict[str, Any]:
    """What a real pending registration answers — also what a collision answers under
    approval mode, so the response never says whether a name or email is taken."""
    return {
        "ok": True,
        "username": users_service.normalize_username(username),
        "email": users_service.normalize_email(email),
        "status": users_service.STATUS_PENDING,
        "requires_approval": True,
        "expires_at": None,
        "valid_days": settings.auth_registration_valid_days,
    }


@router.post("/register", status_code=201)
def register(req: RegisterRequest, request: Request) -> dict[str, Any]:
    settings = get_settings()
    if not registration_enabled(settings):
        raise AppError(
            "auth.registration_disabled",
            "Self-service registration is not available on this console",
        )
    # A-2 (r12-g): the per-peer window counts FAILED attempts only (validation errors,
    # collisions, backlog) and is consulted only after one — behind nginx / the NLB
    # every caller shares one peer address, so a successful registration is never
    # refused by it; a tripped window turns further failures into 429 (rev-r11).
    peer = login_guard.peer_address(request)
    approval = registration_requires_approval(settings)
    db = SessionLocal()
    try:
        try:
            return _register(req, settings, db, peer, approval)
        except AppError as exc:
            if exc.status_code in (400, 409):
                waiting = login_guard.register_retry_after(peer, settings)
                login_guard.record_register_attempt(peer, settings)
                if waiting:
                    raise login_guard.rate_limited_error(waiting, what="registration") from None
            raise
    finally:
        db.close()


def _register(
    req: RegisterRequest, settings: Settings, db: Session, peer: str, approval: bool
) -> dict[str, Any]:
    """The registration itself; `register()` wraps it with the failed-attempt window."""
    if approval and settings.auth_registration_pending_max:
        pending = users_service.pending_count(db)
        if pending >= settings.auth_registration_pending_max:
            logger.warning(
                "registration backlog: pending=%d cap=%d peer=%s — approve or reject "
                "pending accounts in the Users console (auth_registration_pending_max)",
                pending,
                settings.auth_registration_pending_max,
                peer,
            )
            raise AppError(
                "auth.registration_backlog",
                "Registration is paused while an administrator reviews pending "
                "accounts. Try again later.",
                {"pending": pending, "max": settings.auth_registration_pending_max},
                status_code=409,
            )
    try:
        user = users_service.register_user(
            db, req.username, req.email, req.password, settings
        )
    except AppError as exc:
        if exc.code not in ("auth.username_taken", "auth.email_taken"):
            raise
        # Uniform answer for a taken (or reserved) username and a taken email.
        # Under approval mode the caller could not sign in yet anyway, so the
        # response is byte-identical to a real pending registration and no row is
        # created; the journal tells the administrator what actually happened.
        # Under instant activation a fake "ready" answer would strand a legitimate
        # user, so both collisions share ONE 409 code that names neither field.
        logger.info(
            "registration collision: %s username_key=%s peer=%s",
            exc.code,
            users_service.normalize_username(req.username).lower(),
            peer,
        )
        if approval:
            # counted as a failed attempt for the window, but the answer stays the
            # pending shape: a 429 here would tell a prober that the name collided
            login_guard.record_register_attempt(peer, settings)
            return _pending_shaped(req.username, req.email, settings)
        raise AppError(
            "auth.registration_rejected",
            "This username or email cannot be registered. Choose another, or ask an "
            "administrator.",
            status_code=409,
        ) from None
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


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    settings = get_settings()
    response.delete_cookie(
        cookie_name(settings),
        path="/",
        secure=cookie_secure(settings),
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}
