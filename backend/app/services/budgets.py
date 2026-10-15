"""Per-user spend budgets: levels, the effective limit, the period, and the gate.

A **level** is a USD budget per calendar month (UTC), nothing else. People are
assigned a level and optionally a personal amount that replaces it. The effective
limit of a registered account:

    override > assigned level > default level > unlimited

with two exemptions: an **admin** is unlimited until an admin assigns them a level
(or an override), and the **config admin** / **auth-disabled operator** (no
``users`` row) and a ``/v1`` key with no recorded creator are always unlimited.
So nothing is enforced until an administrator creates a level.

**Spend** for a period is the sum of the person's ``usage_events`` rows that no
later row superseded (``services/usage_meter.py`` writes them). The **gate**
(``check``) runs before every billable entry (``core/billing_policy.py``): at or
over the limit it refuses with ``402 budget.exceeded``; a budgeted payer whose
agent model has no price is refused with ``402 budget.model_unpriced`` so an
unpriced model cannot bypass the limit. Work already in flight finishes — the
overshoot is bounded by what was running at the crossing. Neither 402 is
retryable; clients must not treat it like a 429.

Hub-global: one budget per person across every workspace; each box (prod, prod2)
has its own ledger and therefore its own budgets.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.models.budgets import BudgetLevel, UsageEvent
from app.models.ledger import Agent, ApiKey, User
from app.models.platform_settings import PlatformSetting
from app.services import rates

# Sources (usage_events.source) live with the billing table: core/billing_policy.py.

# How the effective limit was reached (people table, My usage).
BASIS_OVERRIDE = "override"
BASIS_LEVEL = "level"
BASIS_DEFAULT = "default_level"
BASIS_UNLIMITED = "unlimited"
BASIS_EXEMPT_ADMIN = "exempt_admin"
BASIS_EXEMPT_PRINCIPAL = "exempt_principal"  # config admin, auth-off operator, NULL key

_ROLE_ADMIN = "admin"
MAX_BUDGET_USD = 1_000_000.0
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
SETTINGS_KEY = "budget_settings"
DEFAULT_SETTINGS: dict[str, float] = {
    # What one turn on an opaque runtime (Strands zip, BYOC without usage, Studio,
    # A2A) is charged until reconciliation replaces it with measured cost.
    "opaque_turn_estimate_usd": 0.01,
}


# ── the period: calendar month, UTC ──────────────────────────────────────────


def period_of(moment: datetime | None = None) -> str:
    """``YYYY-MM`` of the UTC calendar month ``moment`` falls in (now by default)."""
    value = moment or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    value = value.astimezone(UTC)
    return f"{value.year:04d}-{value.month:02d}"


def validate_period(period: str | None) -> str:
    if period is None or period == "":
        return period_of()
    if not _PERIOD_RE.match(period):
        raise AppError("budget.invalid_period", "period must look like YYYY-MM",
                       {"period": period}, status_code=422)
    return period


def period_start(period: str) -> datetime:
    year, month = (int(part) for part in period.split("-"))
    return datetime(year, month, 1, tzinfo=UTC)


def resets_at(period: str) -> str:
    """The instant the period's budget resets: 00:00 UTC on the next month's 1st."""
    year, month = (int(part) for part in period.split("-"))
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return datetime(year, month, 1, tzinfo=UTC).isoformat()


# ── the payer and the effective limit ────────────────────────────────────────


@dataclass(frozen=True)
class Payer:
    """Who pays for a billable action, and their limit this period.

    ``user_id`` is ``users.id`` (None for an exempt principal with no row);
    ``limit_usd`` None means unlimited.
    """

    user_id: str | None
    name: str | None
    limit_usd: float | None = None
    level_id: str | None = None
    level_name: str | None = None
    basis: str = BASIS_UNLIMITED

    @property
    def budgeted(self) -> bool:
        return self.limit_usd is not None


def exempt_payer(name: str | None) -> Payer:
    return Payer(user_id=None, name=name, basis=BASIS_EXEMPT_PRINCIPAL)


def default_level(db: Session) -> BudgetLevel | None:
    return (
        db.query(BudgetLevel)
        .filter(BudgetLevel.is_default.is_(True))
        .order_by(BudgetLevel.created_at)
        .first()
    )


def effective_limit(
    db: Session, user: User, *, default: BudgetLevel | None | bool = False,
) -> tuple[float | None, BudgetLevel | None, str]:
    """``(limit_usd | None, level | None, basis)`` for one account.

    ``default`` lets a caller iterating many users pass the default level it
    already read (``None`` = there is none); ``False`` reads it here.
    """
    level = db.get(BudgetLevel, user.budget_level_id) if user.budget_level_id else None
    override = user.budget_override_usd
    assigned = level is not None or override is not None
    if user.role == _ROLE_ADMIN and not assigned:
        return None, None, BASIS_EXEMPT_ADMIN
    if override is not None:
        return float(override), level, BASIS_OVERRIDE
    if level is not None:
        return float(level.monthly_budget_usd), level, BASIS_LEVEL
    fallback = default_level(db) if default is False else default
    if isinstance(fallback, BudgetLevel):
        return float(fallback.monthly_budget_usd), fallback, BASIS_DEFAULT
    return None, None, BASIS_UNLIMITED


def payer_for_user(db: Session, user: User) -> Payer:
    limit, level, basis = effective_limit(db, user)
    return Payer(
        user_id=user.id, name=user.username, limit_usd=limit,
        level_id=level.id if level is not None else None,
        level_name=level.name if level is not None else None, basis=basis,
    )


def payer_for_user_id(db: Session, user_id: str | None, name: str | None = None) -> Payer:
    """The payer behind a stored ``users.id`` (a job row's starter). A missing or
    deleted account is charged by id but never refused."""
    if not user_id:
        return exempt_payer(name)
    user = db.get(User, user_id)
    if user is None:
        return Payer(user_id=user_id, name=name)
    return payer_for_user(db, user)


def payer_for_identity(db: Session, identity: Any) -> Payer:
    """Console caller → payer. The config admin and the auth-disabled operator
    carry no ``user_id`` and are always exempt."""
    user_id = getattr(identity, "user_id", None)
    name = getattr(identity, "username", None)
    if not user_id:
        return exempt_payer(name)
    return payer_for_user_id(db, user_id, name)


def payer_for_api_key(db: Session, key: ApiKey) -> Payer:
    """``/v1`` call → the key's creator. A key minted by the config admin, the
    auth-disabled operator or before creators were recorded has no creator and
    is exempt."""
    if not key.created_by:
        return exempt_payer(key.created_by_name)
    return payer_for_user_id(db, key.created_by, key.created_by_name)


def payer_of_request(request: Request) -> Payer:
    """The console caller's payer, resolved once per request (the gate caches it)."""
    cached = getattr(request.state, "budget_payer", None)
    if isinstance(cached, Payer):
        return cached
    from app.core.db import SessionLocal
    from app.routers.auth import require_identity

    identity = require_identity(request)
    if not identity.user_id:
        payer = exempt_payer(identity.username)
    else:
        db = SessionLocal()
        try:
            payer = payer_for_identity(db, identity)
        finally:
            db.close()
    request.state.budget_payer = payer
    return payer


# ── spend and the check ──────────────────────────────────────────────────────


def spend(db: Session, user_id: str | None, period: str) -> float:
    """The person's spend in ``period``: the sum of rows nothing superseded.
    Rows with no price (``usd`` NULL) add nothing."""
    if not user_id:
        return 0.0
    total = (
        db.query(func.coalesce(func.sum(UsageEvent.usd), 0.0))
        .filter(
            UsageEvent.user_id == user_id,
            UsageEvent.period == period,
            UsageEvent.superseded_by.is_(None),
        )
        .scalar()
    )
    return float(total or 0.0)


def _exceeded(payer: Payer, used: float, period: str) -> AppError:
    return AppError(
        "budget.exceeded",
        f"monthly budget reached: ${used:.2f} of ${payer.limit_usd:.2f} used; "
        f"it resets on {resets_at(period)[:10]}",
        {
            "used_usd": round(used, 6),
            "limit_usd": payer.limit_usd,
            "period": period,
            "resets_at": resets_at(period),
            "level": payer.level_name,
        },
        status_code=402,
    )


def _unpriced(model_id: str | None) -> AppError:
    if model_id:
        message = (f"model {model_id} has no price yet, so usage of it cannot be counted "
                   "against your budget; ask an administrator to price it on the Models page")
    else:  # an imported Harness: no model id an administrator could price
        message = ("this agent's model is not known to the platform, so its usage cannot "
                   "be counted against your budget; ask an administrator")
    return AppError("budget.model_unpriced", message, {"model_id": model_id}, status_code=402)


def check(
    db: Session,
    payer: Payer,
    *,
    model_id: str | None = None,
    model_unknown: bool = False,
    now: datetime | None = None,
) -> None:
    """Refuse a budgeted payer at or over the limit, or about to use an unpriced
    model. Exempt and unlimited payers are never refused (and cost no query)."""
    if not payer.budgeted:
        return
    period = period_of(now)
    used = spend(db, payer.user_id, period)
    if used >= float(payer.limit_usd or 0.0):
        raise _exceeded(payer, used, period)
    if model_unknown or (model_id and rates.is_unpriced(model_id)):
        raise _unpriced(model_id)


def check_model(payer: Payer, model_id: str | None) -> None:
    """The unpriced-model half of ``check``, for an agent-less billable route that
    knows its model (the Studio AI fix's coding agent): the route gate already
    checked the spend, so this needs no query. Exempt and unlimited payers pass."""
    if payer.budgeted and model_id and rates.is_unpriced(model_id):
        raise _unpriced(model_id)


def agent_model(agent: Agent) -> tuple[str | None, bool]:
    """``(model_id | None, token_metered)`` of an agent: the model its usage is
    priced at, and whether its turns are metered by tokens in-band (Harness,
    the Claude Agent SDK container) rather than charged a flat estimate."""
    from app.schemas.agent import claude_sdk_default_model_id
    from app.services.runtime_discovery import is_discovered_harness

    spec = agent.spec or {}
    model = str(spec.get("model_id") or "").strip() or None
    if model is None and agent.method == "container":
        model = claude_sdk_default_model_id()
    harness = agent.method == "harness" or is_discovered_harness(agent)
    return model, harness or agent.method == "container"


def check_agent(db: Session, payer: Payer, agent: Agent | None) -> None:
    """``check`` with the agent's model: a token-metered agent whose model is
    unknown (an imported Harness) cannot be priced either, so it is refused for a
    budgeted payer like an unpriced one. An opaque runtime with no known model is
    charged its flat per-turn estimate, so it is allowed."""
    if agent is None:
        check(db, payer)
        return
    model, token_metered = agent_model(agent)
    check(db, payer, model_id=model, model_unknown=model is None and token_metered)


def gate_request(request: Request, source: str) -> None:
    """The billing gate for one console request (``enforce_route_policy`` calls it
    for every BILLABLE route, after the role and workspace checks). When the
    route names an agent the caller can see, its model decides the unpriced check;
    an agent-less route checks spend only."""
    del source  # classified for auditing; the check itself is the same for all
    payer = payer_of_request(request)
    if not payer.budgeted:
        return
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        agent = None
        agent_id = (request.path_params or {}).get("agent_id")
        scope = getattr(request.state, "workspace", None)
        if agent_id and scope is not None:
            from app.services.agent_access import agent_in
            from app.services.visibility import Viewer

            agent = agent_in(db, scope.id, agent_id, viewer=Viewer.of_request(request))
        check_agent(db, payer, agent)
    finally:
        db.close()


# ── summaries: My usage and the people table ─────────────────────────────────


def _by_source(db: Session, period: str, *, user_id: str | None,
               user_name: str | None = None) -> list[dict[str, Any]]:
    query = (
        db.query(
            UsageEvent.source,
            UsageEvent.estimated,
            func.coalesce(func.sum(UsageEvent.usd), 0.0),
            func.count(UsageEvent.id),
        )
        .filter(UsageEvent.period == period, UsageEvent.superseded_by.is_(None))
    )
    if user_id:
        query = query.filter(UsageEvent.user_id == user_id)
    else:
        query = query.filter(UsageEvent.user_id.is_(None), UsageEvent.user_name == user_name)
    sources: dict[str, dict[str, Any]] = {}
    for source, estimated, usd, count in query.group_by(
        UsageEvent.source, UsageEvent.estimated
    ):
        entry = sources.setdefault(source, {"source": source, "usd": 0.0,
                                            "estimated_usd": 0.0, "events": 0})
        entry["usd"] += float(usd or 0.0)
        entry["events"] += int(count or 0)
        if estimated:
            entry["estimated_usd"] += float(usd or 0.0)
    out = sorted(sources.values(), key=lambda e: (-e["usd"], e["source"]))
    for entry in out:
        entry["usd"] = round(entry["usd"], 6)
        entry["estimated_usd"] = round(entry["estimated_usd"], 6)
    return out


def _totals(by_source: list[dict[str, Any]], limit: float | None) -> dict[str, Any]:
    used = round(sum(e["usd"] for e in by_source), 6)
    estimated = round(sum(e["estimated_usd"] for e in by_source), 6)
    return {
        "limit_usd": limit,
        "used_usd": used,
        "remaining_usd": None if limit is None else round(max(0.0, limit - used), 6),
        "estimated_usd": estimated,
        "estimated_share": round(estimated / used, 4) if used > 0 else 0.0,
        "percent_used": (
            None if limit is None else (round(used / limit * 100, 2) if limit > 0 else 100.0)
        ),
        "unlimited": limit is None,
    }


def usage_summary(db: Session, payer: Payer, period: str | None = None) -> dict[str, Any]:
    """My usage: the caller's own spend this period, never anyone else's. An exempt
    principal (no ``users`` row) sees the rows charged under its name."""
    period = validate_period(period)
    rows = _by_source(db, period, user_id=payer.user_id, user_name=payer.name)
    return {
        "user_id": payer.user_id,
        "username": payer.name,
        "level": ({"id": payer.level_id, "name": payer.level_name}
                  if payer.level_id else None),
        "basis": payer.basis,
        **_totals(rows, payer.limit_usd),
        "period": period,
        "resets_at": resets_at(period),
        "by_source": rows,
    }


def people(db: Session, period: str | None = None) -> dict[str, Any]:
    """The admin people table: every account with its level, limit and spend, plus
    the spend charged to principals with no account (config admin, operator,
    NULL-creator keys)."""
    period = validate_period(period)
    fallback = default_level(db)
    db.query(BudgetLevel).all()  # load every level once (identity map)
    spend_rows = (
        db.query(UsageEvent.user_id, UsageEvent.user_name, UsageEvent.source,
                 UsageEvent.estimated, func.coalesce(func.sum(UsageEvent.usd), 0.0),
                 func.count(UsageEvent.id))
        .filter(UsageEvent.period == period, UsageEvent.superseded_by.is_(None))
        .group_by(UsageEvent.user_id, UsageEvent.user_name, UsageEvent.source,
                  UsageEvent.estimated)
        .all()
    )
    per_user: dict[str | None, dict[str, dict[str, Any]]] = {}
    exempt: dict[str | None, dict[str, dict[str, Any]]] = {}
    for user_id, user_name, source, estimated, usd, count in spend_rows:
        bucket = (per_user.setdefault(user_id, {}) if user_id
                  else exempt.setdefault(user_name, {}))
        entry = bucket.setdefault(source, {"source": source, "usd": 0.0,
                                           "estimated_usd": 0.0, "events": 0})
        entry["usd"] += float(usd or 0.0)
        entry["events"] += int(count or 0)
        if estimated:
            entry["estimated_usd"] += float(usd or 0.0)

    def finish(sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        out = sorted(sources.values(), key=lambda e: (-e["usd"], e["source"]))
        for entry in out:
            entry["usd"] = round(entry["usd"], 6)
            entry["estimated_usd"] = round(entry["estimated_usd"], 6)
        return out

    rows: list[dict[str, Any]] = []
    for user in db.query(User).order_by(User.username_key):
        # the levels are already in the session's identity map: no query per user
        limit, shown, basis = effective_limit(db, user, default=fallback)
        by_source = finish(per_user.get(user.id, {}))
        rows.append({
            "user_id": user.id,
            "username": user.username,
            "role": user.role,
            "status": user.status,
            "level": {"id": shown.id, "name": shown.name} if shown is not None else None,
            "assigned_level_id": user.budget_level_id,
            "override_usd": user.budget_override_usd,
            "basis": basis,
            **_totals(by_source, limit),
            "top_source": by_source[0]["source"] if by_source else None,
            "by_source": by_source,
        })
    known = {row["user_id"] for row in rows}
    unattributed = []
    for user_id, sources in per_user.items():
        if user_id not in known:  # a deleted account's spend stays visible
            by_source = finish(sources)
            unattributed.append({"user_id": user_id, "user_name": None,
                                 **_totals(by_source, None), "by_source": by_source})
    for name, sources in exempt.items():
        by_source = finish(sources)
        unattributed.append({"user_id": None, "user_name": name,
                             **_totals(by_source, None), "by_source": by_source})
    return {
        "period": period,
        "resets_at": resets_at(period),
        "people": rows,
        "unattributed": unattributed,
    }


# ── levels ───────────────────────────────────────────────────────────────────


def _budget_amount(value: Any, field: str = "monthly_budget_usd") -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise AppError("budget.invalid_amount", f"{field} must be a number",
                       {"field": field}, status_code=422) from None
    if not math.isfinite(amount) or amount < 0 or amount > MAX_BUDGET_USD:
        raise AppError("budget.invalid_amount",
                       f"{field} must be between 0 and {MAX_BUDGET_USD:g}",
                       {"field": field}, status_code=422)
    return round(amount, 6)


def _level_name(value: Any) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 64:
        raise AppError("budget.invalid_level_name", "a level name needs 1 to 64 characters",
                       status_code=422)
    return name


def _user_counts(db: Session) -> dict[str, int]:
    return {
        level_id: count
        for level_id, count in db.query(User.budget_level_id, func.count(User.id))
        .filter(User.budget_level_id.is_not(None))
        .group_by(User.budget_level_id)
    }


def level_out(level: BudgetLevel, user_count: int = 0) -> dict[str, Any]:
    return {
        "id": level.id,
        "name": level.name,
        "monthly_budget_usd": level.monthly_budget_usd,
        "is_default": bool(level.is_default),
        "user_count": user_count,
        "created_by": level.created_by,
        "created_by_name": level.created_by_name,
        "created_at": level.created_at.isoformat() if level.created_at else None,
        "updated_at": level.updated_at.isoformat() if level.updated_at else None,
    }


def list_levels(db: Session) -> list[dict[str, Any]]:
    counts = _user_counts(db)
    levels = db.query(BudgetLevel).order_by(BudgetLevel.monthly_budget_usd, BudgetLevel.name)
    return [level_out(level, counts.get(level.id, 0)) for level in levels]


def get_level(db: Session, level_id: str) -> BudgetLevel:
    level = db.get(BudgetLevel, level_id)
    if level is None:
        raise NotFoundError("budget.level_not_found", "budget level not found",
                            {"level_id": level_id})
    return level


def _require_unique_name(db: Session, name: str, exclude: str | None = None) -> None:
    clash = (
        db.query(BudgetLevel)
        .filter(func.lower(BudgetLevel.name) == name.lower())
        .filter(BudgetLevel.id != (exclude or ""))
        .first()
    )
    if clash is not None:
        raise AppError("budget.level_name_taken", f"a level named {name!r} already exists",
                       {"name": name}, status_code=409)


def _clear_other_defaults(db: Session, keep: str) -> None:
    for other in db.query(BudgetLevel).filter(BudgetLevel.is_default.is_(True),
                                              BudgetLevel.id != keep):
        other.is_default = False


def create_level(
    db: Session, *, name: Any, monthly_budget_usd: Any, is_default: bool = False,
    creator: Any = None,
) -> dict[str, Any]:
    clean = _level_name(name)
    _require_unique_name(db, clean)
    level = BudgetLevel(
        name=clean, monthly_budget_usd=_budget_amount(monthly_budget_usd),
        is_default=bool(is_default),
        created_by=getattr(creator, "user_id", None),
        created_by_name=getattr(creator, "name", None),
    )
    db.add(level)
    db.flush()
    if level.is_default:
        _clear_other_defaults(db, level.id)
    db.commit()
    return level_out(level, 0)


def update_level(db: Session, level_id: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Apply only the keys sent (``name``, ``monthly_budget_usd``, ``is_default``)."""
    level = get_level(db, level_id)
    if "name" in changes:
        clean = _level_name(changes["name"])
        _require_unique_name(db, clean, exclude=level.id)
        level.name = clean
    if "monthly_budget_usd" in changes:
        level.monthly_budget_usd = _budget_amount(changes["monthly_budget_usd"])
    if "is_default" in changes:
        level.is_default = bool(changes["is_default"])
        if level.is_default:
            _clear_other_defaults(db, level.id)
    level.updated_at = datetime.now(UTC)
    db.commit()
    return level_out(level, _user_counts(db).get(level.id, 0))


def delete_level(db: Session, level_id: str) -> None:
    """Refused while any account is assigned to it: unassign them first, so no
    one silently falls through to the default or to unlimited."""
    level = get_level(db, level_id)
    assigned = _user_counts(db).get(level.id, 0)
    if assigned:
        raise AppError(
            "budget.level_in_use",
            f"{assigned} account(s) are assigned to level {level.name!r}; "
            "move them to another level first",
            {"users": assigned, "level_id": level.id},
            status_code=409,
        )
    db.delete(level)
    db.commit()


def assign_user(
    db: Session, user_id: str, *, level_id: str | None, override_usd: Any,
) -> dict[str, Any]:
    """Set an account's level (None = none) and personal amount (None = none)."""
    user = db.get(User, user_id)
    if user is None:
        raise NotFoundError("users.not_found", "user not found", {"user_id": user_id})
    if level_id:
        get_level(db, level_id)
    user.budget_level_id = level_id or None
    user.budget_override_usd = (
        None if override_usd is None else _budget_amount(override_usd, "override_usd")
    )
    db.commit()
    payer = payer_for_user(db, user)
    return {
        "user_id": user.id,
        "username": user.username,
        "assigned_level_id": user.budget_level_id,
        "override_usd": user.budget_override_usd,
        "level": ({"id": payer.level_id, "name": payer.level_name}
                  if payer.level_id else None),
        "limit_usd": payer.limit_usd,
        "basis": payer.basis,
        "unlimited": payer.limit_usd is None,
    }


# ── budget settings (platform_settings key budget_settings) ──────────────────


def _stored_settings(db: Session) -> tuple[dict[str, Any], PlatformSetting | None]:
    row = db.get(PlatformSetting, SETTINGS_KEY)
    if row is None or not row.value:
        return {}, row
    try:
        value = json.loads(row.value)
    except ValueError:
        return {}, row
    return (value if isinstance(value, dict) else {}), row


def budget_settings(db: Session) -> dict[str, float]:
    stored, _ = _stored_settings(db)
    out = dict(DEFAULT_SETTINGS)
    for key, value in stored.items():
        if key in out:
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                continue
    return out


def budget_settings_view(db: Session) -> dict[str, Any]:
    _, row = _stored_settings(db)
    return {
        **budget_settings(db),
        "defaults": dict(DEFAULT_SETTINGS),
        "updated_by": row.updated_by if row is not None else None,
        "updated_at": row.updated_at.isoformat() if row is not None else None,
    }


def set_budget_settings(db: Session, values: dict[str, Any], actor: str) -> dict[str, Any]:
    """Merge the given keys; ``null`` resets one to its default."""
    stored, row = _stored_settings(db)
    for key, value in values.items():
        if key not in DEFAULT_SETTINGS:
            raise AppError("budget.unknown_setting", f"unknown budget setting {key!r}",
                           {"key": key}, status_code=422)
        if value is None:
            stored.pop(key, None)
        else:
            stored[key] = _budget_amount(value, key)
    if row is None:
        row = PlatformSetting(key=SETTINGS_KEY)
        db.add(row)
    row.value = json.dumps(stored, sort_keys=True)
    row.updated_by = (actor or "")[:64]
    row.updated_at = datetime.now(UTC)
    db.commit()
    return budget_settings_view(db)


# ── models seen without a price (the admin price editor lists them first) ────


def unpriced_models(db: Session) -> list[dict[str, Any]]:
    """Model ids the platform uses that the merged price map cannot price: from
    metered usage (rows recorded with no price), from agents' specs, and from the
    platform model roles. The Spend page's own ``unpriced_models`` (span-derived)
    complements this list on the frontend."""
    from app.core.config import get_settings
    from app.services import model_settings

    found: dict[str, set[str]] = {}

    def note(model_id: Any, where: str) -> None:
        value = str(model_id or "").strip()
        if value and rates.is_unpriced(value):
            found.setdefault(value, set()).add(where)

    for (model_id,) in (
        db.query(UsageEvent.model_id)
        .filter(UsageEvent.usd.is_(None), UsageEvent.model_id.is_not(None))
        .distinct()
    ):
        note(model_id, "usage")
    for agent in db.query(Agent).filter(Agent.status != "deleted"):
        note((agent.spec or {}).get("model_id"), "agents")
    settings = get_settings()
    for role in model_settings.ROLES:
        note(getattr(settings, role.field, None), "roles")
    return [{"model_id": key, "seen_in": sorted(where)} for key, where in sorted(found.items())]
