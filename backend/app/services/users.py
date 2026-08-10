"""Console account service — registration, credentials, and admin management.

The built-in admin lives in settings (`auth_username`/`auth_password`); every
*other* console account is a `users` row created here. Passwords are hashed with
a stdlib KDF (no passlib/bcrypt dependency) and accounts are time-boxed:
`expires_at` is authoritative and re-checked on every guarded request, so an
expired or disabled account loses access immediately.
"""

import base64
import hashlib
import hmac
import re
import secrets
import string
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import AppError, NotFoundError
from app.models.ledger import User

# Member-grantable agent-management capabilities. Defined here (not in
# routers.auth, which imports this module) so both auth and the users console
# share one list. `users.permissions` stores only explicit False entries; a
# missing key or a None column means granted.
AGENT_PERMISSIONS = (
    "agents.deploy",
    "agents.import",
    "agents.delete",
    "agents.convert",
    "eval.run",
)

# --- password hashing -------------------------------------------------------

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 390_000
_SALT_BYTES = 16
_DK_BYTES = 32
# A stable hash of a value nobody can submit: used to burn the same PBKDF2 work
# on unknown usernames so response timing does not disclose account existence.
_DUMMY_HASH_PASSWORD = "launchpad-dummy-verify-target"


def hash_password(password: str) -> str:
    """Return `pbkdf2_sha256$<iters>$<salt_b64>$<dk_b64>`."""
    salt = secrets.token_bytes(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS, _DK_BYTES)
    return "$".join(
        [
            _ALGO,
            str(_ITERATIONS),
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(dk).decode("ascii"),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of `password` against a stored hash string."""
    parts = (stored or "").split("$")
    if len(parts) != 4 or parts[0] != _ALGO or not parts[1].isdigit():
        return False
    try:
        salt = base64.urlsafe_b64decode(parts[2].encode("ascii"))
        expected = base64.urlsafe_b64decode(parts[3].encode("ascii"))
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, int(parts[1]), len(expected) or _DK_BYTES
    )
    return hmac.compare_digest(dk, expected)


def _burn_verify(password: str) -> None:
    """Spend one PBKDF2 verification on a throwaway hash (timing equalizer)."""
    verify_password(password, hash_password(_DUMMY_HASH_PASSWORD))


def generate_password(length: int = 14) -> str:
    """Admin password reset: a readable-but-random password shown once."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --- validation -------------------------------------------------------------

_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9]([A-Za-z0-9.-]{0,251})\.[A-Za-z]{2,24}$"
)
MIN_PASSWORD_LENGTH = 8


def normalize_username(username: str) -> str:
    return (username or "").strip()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _domain_matches(domain: str, entry: str) -> bool:
    entry = entry.strip().lower().lstrip("@").lstrip(".")
    if not entry:
        return False
    # exact domain or any subdomain of it, so regional variants of a blocked
    # base domain (mail.gmail.com) cannot slip through
    return domain == entry or domain.endswith(f".{entry}")


def validate_username(username: str, settings: Settings) -> str:
    name = normalize_username(username)
    if not _USERNAME_RE.match(name):
        raise AppError(
            "auth.invalid_username",
            "Username must be 3-32 characters of letters, digits, dot, underscore or hyphen",
        )
    if name.lower() == settings.auth_username.strip().lower():
        raise AppError("auth.username_taken", "This username is reserved", status_code=409)
    return name


def validate_email(email: str, settings: Settings) -> str:
    address = normalize_email(email)
    if not _EMAIL_RE.match(address):
        raise AppError("auth.invalid_email", "Enter a valid email address")
    domain = address.rpartition("@")[2]
    allowed = [d for d in settings.auth_allowed_email_domains if d.strip()]
    if allowed:
        if not any(_domain_matches(domain, entry) for entry in allowed):
            raise AppError(
                "auth.email_domain_blocked",
                "Registration is limited to approved company email domains",
            )
        return address
    if any(_domain_matches(domain, entry) for entry in settings.auth_blocked_email_domains):
        raise AppError(
            "auth.email_domain_blocked",
            "Use your company email address — public mailbox providers are not accepted",
        )
    return address


def validate_password(password: str) -> str:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AppError(
            "auth.weak_password",
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    return password


# --- state helpers ----------------------------------------------------------


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; re-attach UTC before comparing."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def is_expired(user: User, now: datetime | None = None) -> bool:
    expires_at = as_utc(user.expires_at)
    if expires_at is None:
        return False
    return expires_at <= (now or datetime.now(UTC))


STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DISABLED = "disabled"
STATUSES = (STATUS_PENDING, STATUS_ACTIVE, STATUS_DISABLED)


def account_state(user: User, now: datetime | None = None) -> str:
    """`pending` | `disabled` | `expired` | `active` — derived, never stored."""
    if user.status == STATUS_PENDING:
        return STATUS_PENDING
    if user.status != STATUS_ACTIVE:
        return STATUS_DISABLED
    return "expired" if is_expired(user, now) else STATUS_ACTIVE


def days_remaining(user: User, now: datetime | None = None) -> int | None:
    """Whole days left before expiry (0 once lapsed); None = never expires."""
    expires_at = as_utc(user.expires_at)
    if expires_at is None:
        return None
    seconds = (expires_at - (now or datetime.now(UTC))).total_seconds()
    return max(0, int(seconds // 86400))


def serialize(user: User, now: datetime | None = None) -> dict[str, Any]:
    """Console-facing shape. The password hash never leaves the backend."""
    moment = now or datetime.now(UTC)
    expires_at = as_utc(user.expires_at)
    last_login = as_utc(user.last_login_at)
    return {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "state": account_state(user, moment),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "days_remaining": days_remaining(user, moment),
        "created_at": (as_utc(user.created_at) or moment).isoformat(),
        "last_login_at": last_login.isoformat() if last_login else None,
        "login_count": user.login_count,
        "created_by": user.created_by,
        "permissions": effective_permissions(user),
    }


def effective_permissions(user: User) -> dict[str, bool]:
    """Full permission map as it will be enforced (admins hold everything)."""
    overrides = user.permissions or {}
    return {
        key: user.role == "admin" or overrides.get(key) is not False
        for key in AGENT_PERMISSIONS
    }


# --- lookups ----------------------------------------------------------------


def find_by_username(db: Session, username: str) -> User | None:
    key = normalize_username(username).lower()
    if not key:
        return None
    return db.scalars(select(User).where(User.username_key == key)).first()


def find_by_email(db: Session, email: str) -> User | None:
    address = normalize_email(email)
    if not address:
        return None
    return db.scalars(select(User).where(User.email == address)).first()


def get_user(db: Session, user_id: str) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("users.not_found", "User not found")
    return user


# --- registration / authentication ------------------------------------------


def register_user(
    db: Session,
    username: str,
    email: str,
    password: str,
    settings: Settings | None = None,
    created_by: str = "self",
    role: str = "member",
    valid_days: int | None = None,
) -> User:
    current = settings or get_settings()
    name = validate_username(username, current)
    address = validate_email(email, current)
    validate_password(password)

    if find_by_username(db, name) is not None:
        raise AppError("auth.username_taken", "This username is already taken", status_code=409)
    if find_by_email(db, address) is not None:
        raise AppError("auth.email_taken", "This email is already registered", status_code=409)

    days = current.auth_registration_valid_days if valid_days is None else valid_days
    # With approval required the clock only starts once an admin approves, so the
    # pending account carries no validity window yet.
    pending = current.auth_registration_require_approval and created_by == "self"
    user = User(
        username=name,
        username_key=name.lower(),
        email=address,
        password_hash=hash_password(password),
        role=role,
        status=STATUS_PENDING if pending else STATUS_ACTIVE,
        expires_at=None if pending or not days else datetime.now(UTC) + timedelta(days=days),
        created_by=created_by,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, username: str, password: str) -> User:
    """Return the account for valid credentials.

    Raises `auth.invalid_credentials` for unknown user / wrong password, and the
    distinct `auth.account_disabled` / `auth.account_expired` once the
    credentials themselves check out (the caller proved account ownership, so
    telling them *why* they cannot sign in discloses nothing new).
    """
    user = find_by_username(db, username)
    if user is None:
        _burn_verify(password)
        raise AppError(
            "auth.invalid_credentials", "Invalid username or password", status_code=401
        )
    if not verify_password(password, user.password_hash):
        raise AppError(
            "auth.invalid_credentials", "Invalid username or password", status_code=401
        )
    if user.status == STATUS_PENDING:
        raise AppError(
            "auth.account_pending",
            "This account is waiting for administrator approval",
            status_code=401,
        )
    if user.status != STATUS_ACTIVE:
        raise AppError("auth.account_disabled", "This account is disabled", status_code=401)
    if is_expired(user):
        raise AppError("auth.account_expired", "This account has expired", status_code=401)
    return user


def record_login(db: Session, user: User) -> None:
    user.last_login_at = datetime.now(UTC)
    user.login_count = (user.login_count or 0) + 1
    db.commit()


# --- admin management -------------------------------------------------------

STATUS_FILTERS = ("all", "pending", "active", "expired", "disabled")


@dataclass(frozen=True)
class UserPage:
    items: list[dict[str, Any]]
    total: int
    limit: int
    offset: int


def list_users(
    db: Session,
    q: str | None = None,
    status: str = "all",
    limit: int = 50,
    offset: int = 0,
) -> UserPage:
    now = datetime.now(UTC)
    stmt = select(User)
    if q:
        needle = f"%{q.strip().lower()}%"
        stmt = stmt.where(
            or_(func.lower(User.username).like(needle), User.email.like(needle))
        )
    rows = list(db.scalars(stmt.order_by(User.created_at.desc())))
    # expiry is time-derived, so status filtering happens in Python against the
    # same helper the auth path uses (one definition of "expired")
    if status in (STATUS_PENDING, STATUS_ACTIVE, "expired", STATUS_DISABLED):
        rows = [row for row in rows if account_state(row, now) == status]
    total = len(rows)
    window = rows[offset : offset + limit] if limit > 0 else rows[offset:]
    return UserPage(
        items=[serialize(row, now) for row in window],
        total=total,
        limit=limit,
        offset=offset,
    )


def compute_stats(db: Session, settings: Settings | None = None) -> dict[str, Any]:
    current = settings or get_settings()
    now = datetime.now(UTC)
    rows: Sequence[User] = list(db.scalars(select(User)))
    states = [account_state(row, now) for row in rows]

    def _created(row: User) -> datetime:
        return as_utc(row.created_at) or now

    week_ago = now - timedelta(days=7)
    expiring_soon = 0
    for row, state in zip(rows, states, strict=True):
        expires_at = as_utc(row.expires_at)
        if (
            state == STATUS_ACTIVE
            and expires_at is not None
            and expires_at - now <= timedelta(days=3)
        ):
            expiring_soon += 1

    day_keys = [(now - timedelta(days=offset)).date() for offset in range(13, -1, -1)]
    counts = dict.fromkeys(day_keys, 0)
    for row in rows:
        day = _created(row).date()
        if day in counts:
            counts[day] += 1

    domains: dict[str, int] = {}
    for row in rows:
        domain = row.email.rpartition("@")[2]
        if domain:
            domains[domain] = domains.get(domain, 0) + 1
    top_domains = sorted(domains.items(), key=lambda item: (-item[1], item[0]))[:5]

    return {
        "total": len(rows),
        "pending": states.count(STATUS_PENDING),
        "active": states.count(STATUS_ACTIVE),
        "expired": states.count("expired"),
        "disabled": states.count(STATUS_DISABLED),
        "expiring_soon": expiring_soon,
        "registered_last_7d": sum(1 for row in rows if _created(row) >= week_ago),
        "active_last_7d": sum(
            1
            for row in rows
            if row.last_login_at is not None and (as_utc(row.last_login_at) or now) >= week_ago
        ),
        "registrations": [
            {"date": day.isoformat(), "count": counts[day]} for day in day_keys
        ],
        "top_domains": [{"domain": domain, "count": count} for domain, count in top_domains],
        "valid_days": current.auth_registration_valid_days,
    }


def extend_validity(user: User, days: int) -> None:
    """Add `days` on top of the remaining validity (revive from now if lapsed)."""
    now = datetime.now(UTC)
    base = as_utc(user.expires_at) or now
    user.expires_at = max(base, now) + timedelta(days=days)


def apply_patch(
    db: Session,
    user: User,
    patch: dict[str, Any],
    settings: Settings | None = None,
) -> str | None:
    """Apply an admin patch in place. Returns a generated password, if any.

    `patch` only carries keys the caller actually sent, so `{"expires_at": None}`
    means "never expires" while an omitted key leaves the field alone.
    """
    current = settings or get_settings()
    generated: str | None = None
    if "status" in patch:
        status = patch["status"]
        if status not in STATUSES:
            raise AppError(
                "users.invalid_status", f"Status must be one of {', '.join(STATUSES)}"
            )
        approving = user.status == STATUS_PENDING and status == STATUS_ACTIVE
        user.status = status
        # approving a pending account starts its validity window, unless the same
        # patch sets the expiry explicitly
        if approving and user.expires_at is None and not {"expires_at", "extend_days"} & set(patch):
            user.expires_at = datetime.now(UTC) + timedelta(
                days=current.auth_registration_valid_days
            )
    if "role" in patch:
        role = patch["role"]
        if role not in ("admin", "member"):
            raise AppError("users.invalid_role", "Role must be admin or member")
        user.role = role
    if "extend_days" in patch and patch["extend_days"] is not None:
        days = int(patch["extend_days"])
        if not 1 <= days <= 3650:
            raise AppError("users.invalid_extension", "Extension must be 1-3650 days")
        extend_validity(user, days)
    if "expires_at" in patch:
        value = patch["expires_at"]
        user.expires_at = as_utc(value) if isinstance(value, datetime) else None
    if "password" in patch:
        # an explicit null asks the platform to generate one, which is the only
        # case the caller gets a password back (shown once in the console)
        chosen = patch["password"]
        password = chosen or generate_password()
        validate_password(password)
        user.password_hash = hash_password(password)
        generated = None if chosen else password
    if "permissions" in patch:
        user.permissions = _normalized_permissions(patch["permissions"])
    db.commit()
    db.refresh(user)
    return generated


def _normalized_permissions(value: Any) -> dict[str, bool] | None:
    """Validate a full/partial permission map; keep only explicit denials.

    Unsent keys count as granted, so an all-granted map normalizes to None and
    permission keys added later stay default-on for every account.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AppError("users.invalid_permissions", "permissions must be an object")
    unknown = sorted(set(value) - set(AGENT_PERMISSIONS))
    if unknown:
        raise AppError(
            "users.invalid_permissions",
            f"Unknown permission key(s): {', '.join(unknown)}",
        )
    if not all(isinstance(flag, bool) for flag in value.values()):
        raise AppError("users.invalid_permissions", "permission values must be booleans")
    denied = {key: False for key, flag in value.items() if flag is False}
    return denied or None


def delete_user(db: Session, user: User) -> None:
    db.delete(user)
    db.commit()
