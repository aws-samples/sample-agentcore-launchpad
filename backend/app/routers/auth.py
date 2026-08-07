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
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError, envelope
from app.services import users as users_service

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "launchpad_session"
SESSION_TTL_SECONDS = 12 * 3600
_COOKIE_VERSION = "1"

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
    plain HTTP, and a `Secure` cookie there is silently never sent back.
    """
    current = settings or get_settings()
    return current.auth_cookie_secure or current.run_mode == "prod"


def registration_requires_approval(settings: Settings | None = None) -> bool:
    return (settings or get_settings()).auth_registration_require_approval


def registration_enabled(settings: Settings | None = None) -> bool:
    current = settings or get_settings()
    # With the gate disabled the console is already open, so there is nothing to
    # register an account for.
    return enabled(current) and current.auth_registration_enabled


def _signing_key(settings: Settings | None = None) -> bytes:
    current = settings or get_settings()
    material = (
        f"agentcore-launchpad-session:{current.auth_username}:{_password(current)}"
    )
    return hashlib.sha256(material.encode("utf-8")).digest()


def _encode_payload(subject: str, expiry: int) -> str:
    raw = f"{_COOKIE_VERSION}:{subject}:{expiry}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sign(payload: str, settings: Settings | None = None) -> str:
    signature = hmac.new(
        _signing_key(settings), payload.encode("ascii"), hashlib.sha256
    )
    return f"{payload}.{signature.hexdigest()}"


def _issue(subject: str, expiry: int, settings: Settings | None = None) -> str:
    return _sign(_encode_payload(subject, expiry), settings)


def _decode(cookie: str | None, settings: Settings | None = None) -> tuple[str, int] | None:
    """Return `(subject, expiry)` for an authentic, unexpired cookie."""
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
    subject, _, expiry_text = rest.rpartition(":")
    if version != _COOKIE_VERSION or not subject or not expiry_text.isdigit():
        return None
    expiry = int(expiry_text)
    return (subject, expiry) if expiry > time.time() else None


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
    decoded = _decode(request.cookies.get(COOKIE_NAME), current)
    if decoded is None:
        return None
    subject, _ = decoded
    if hmac.compare_digest(subject.encode("utf-8"), current.auth_username.encode("utf-8")):
        return Identity(username=current.auth_username, role=ROLE_ADMIN)

    owned = db is None
    session = db or SessionLocal()
    try:
        user = users_service.find_by_username(session, subject)
        if user is None or user.status != "active" or users_service.is_expired(user):
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
            "role": None,
            "email": None,
            "account_expires_at": None,
            "permissions": [],
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
        **_identity_fields(identity if required else None),
    }


@router.post("/login")
def login(req: LoginRequest, response: Response) -> dict[str, Any]:
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

    username_ok = hmac.compare_digest(
        req.username.strip().encode("utf-8"),
        settings.auth_username.encode("utf-8"),
    )
    password_ok = hmac.compare_digest(
        req.password.encode("utf-8"),
        password.encode("utf-8"),
    )
    if username_ok and password_ok:
        identity = Identity(username=settings.auth_username, role=ROLE_ADMIN)
    elif username_ok:
        # the built-in admin never falls through to the ledger
        raise AppError(
            "auth.invalid_credentials", "Invalid username or password", status_code=401
        )
    else:
        db = SessionLocal()
        try:
            user = users_service.authenticate(db, req.username, req.password)
            users_service.record_login(db, user)
            identity = Identity(
                username=user.username,
                role=user.role,
                email=user.email,
                account_expires_at=users_service.as_utc(user.expires_at),
                user_id=user.id,
                permissions=granted_permissions(user.permissions),
            )
        finally:
            db.close()

    expiry = int(time.time()) + SESSION_TTL_SECONDS
    if identity.account_expires_at is not None:
        # never outlive the account itself
        expiry = min(expiry, int(identity.account_expires_at.timestamp()))
    max_age = max(1, expiry - int(time.time()))
    response.set_cookie(
        COOKIE_NAME,
        _issue(identity.username, expiry, settings),
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
        **_identity_fields(identity),
    }


@router.post("/register", status_code=201)
def register(req: RegisterRequest) -> dict[str, Any]:
    settings = get_settings()
    if not registration_enabled(settings):
        raise AppError(
            "auth.registration_disabled",
            "Self-service registration is not available on this console",
        )
    db = SessionLocal()
    try:
        user = users_service.register_user(
            db, req.username, req.email, req.password, settings
        )
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


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    settings = get_settings()
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=cookie_secure(settings),
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}
