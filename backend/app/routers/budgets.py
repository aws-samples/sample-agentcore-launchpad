"""Spend budgets, model prices and AgentCore rates (services/budgets.py, services/rates.py).

Administrator routes (``ROUTE_POLICY``: ADMIN):

- ``GET/POST /api/budgets/levels``, ``PATCH/DELETE /api/budgets/levels/{level_id}``:
  level CRUD; deleting a level that still has accounts assigned is refused (409).
- ``GET /api/budgets/people?period=YYYY-MM``: every account's level, limit, spend
  (by source) and remaining budget, plus spend charged to principals with no account.
- ``PUT /api/budgets/users/{user_id}``: an account's level and personal amount.
- ``GET/PUT /api/budgets/settings``: the flat per-turn estimate for opaque runtimes.
- ``GET /api/admin/model-prices``, ``PUT/DELETE /api/admin/model-prices/{model_id}``:
  the price editor — a model id may carry dots, colons and slashes, so the route
  takes the rest of the path.
- ``GET/PUT /api/admin/agentcore-rates``: AgentCore fee rates and calibration.

Member route (MEMBER): ``GET /api/me/usage`` — the caller's own usage, never anyone
else's.

All hub-global (``WORKSPACE_EXEMPT``): one budget per person across workspaces.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import require_identity
from app.services import budgets, rates
from app.services.creators import Creator
from app.services.observability import CACHE_READ_FACTOR, CACHE_WRITE_FACTOR

router = APIRouter(tags=["budgets"])


# ── levels ───────────────────────────────────────────────────────────────────


class LevelIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    monthly_budget_usd: float = Field(ge=0, le=budgets.MAX_BUDGET_USD, allow_inf_nan=False)
    is_default: bool = False


class LevelPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    monthly_budget_usd: float | None = Field(
        default=None, ge=0, le=budgets.MAX_BUDGET_USD, allow_inf_nan=False,
    )
    is_default: bool | None = None


@router.get("/api/budgets/levels")
def list_levels(db: Session = Depends(get_db)) -> dict[str, Any]:
    levels = budgets.list_levels(db)
    default = next((level["id"] for level in levels if level["is_default"]), None)
    return {"levels": levels, "default_level_id": default}


@router.post("/api/budgets/levels", status_code=201)
def create_level(
    body: LevelIn, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    return budgets.create_level(
        db, name=body.name, monthly_budget_usd=body.monthly_budget_usd,
        is_default=body.is_default, creator=Creator.of_request(request),
    )


@router.patch("/api/budgets/levels/{level_id}")
def update_level(
    level_id: str, body: LevelPatch, db: Session = Depends(get_db)
) -> dict[str, Any]:
    changes = {
        key: getattr(body, key) for key in body.model_fields_set
        if getattr(body, key) is not None
    }
    return budgets.update_level(db, level_id, changes)


@router.delete("/api/budgets/levels/{level_id}")
def delete_level(level_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    budgets.delete_level(db, level_id)
    return {"deleted": True, "id": level_id}


# ── people and assignment ────────────────────────────────────────────────────


@router.get("/api/budgets/people")
def people(period: str | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    return budgets.people(db, period)


class AssignmentIn(BaseModel):
    """Both keys are the full assignment: ``null`` clears that part."""

    level_id: str | None = Field(default=None, max_length=32)
    override_usd: float | None = Field(
        default=None, ge=0, le=budgets.MAX_BUDGET_USD, allow_inf_nan=False,
    )


@router.put("/api/budgets/users/{user_id}")
def assign_user(
    user_id: str, body: AssignmentIn, db: Session = Depends(get_db)
) -> dict[str, Any]:
    return budgets.assign_user(
        db, user_id, level_id=body.level_id, override_usd=body.override_usd,
    )


# ── budget settings ──────────────────────────────────────────────────────────


class BudgetSettingsIn(BaseModel):
    opaque_turn_estimate_usd: float | None = Field(
        default=None, ge=0, le=100, allow_inf_nan=False,
    )


@router.get("/api/budgets/settings")
def get_budget_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    return budgets.budget_settings_view(db)


@router.put("/api/budgets/settings")
def put_budget_settings(
    body: BudgetSettingsIn, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    values = {key: getattr(body, key) for key in body.model_fields_set}
    return budgets.set_budget_settings(db, values, require_identity(request).username)


# ── model prices ─────────────────────────────────────────────────────────────


class PriceIn(BaseModel):
    """USD per 1M tokens. Cache rates default to input × 0.1 (read) and × 1.25
    (write) when omitted."""

    input: float = Field(ge=0, le=rates.MAX_PRICE_PER_MTOK, allow_inf_nan=False)
    output: float = Field(ge=0, le=rates.MAX_PRICE_PER_MTOK, allow_inf_nan=False)
    cache_read: float | None = Field(
        default=None, ge=0, le=rates.MAX_PRICE_PER_MTOK, allow_inf_nan=False,
    )
    cache_write: float | None = Field(
        default=None, ge=0, le=rates.MAX_PRICE_PER_MTOK, allow_inf_nan=False,
    )


@router.get("/api/admin/model-prices")
def model_prices(db: Session = Depends(get_db)) -> dict[str, Any]:
    return {
        "prices": rates.price_table(db),
        "unpriced_models": budgets.unpriced_models(db),
        "cache_factors": {"cache_read": CACHE_READ_FACTOR, "cache_write": CACHE_WRITE_FACTOR},
    }


@router.put("/api/admin/model-prices/{model_id:path}")
def set_model_price(
    model_id: str, body: PriceIn, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    return rates.set_price_override(
        db, model_id, body.model_dump(), require_identity(request).username,
    )


@router.delete("/api/admin/model-prices/{model_id:path}")
def clear_model_price(
    model_id: str, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    return rates.clear_price_override(db, model_id, require_identity(request).username)


# ── AgentCore rates ──────────────────────────────────────────────────────────


class AgentCoreRatesIn(BaseModel):
    """Keys to change; a ``null`` value resets that key to its default."""

    rates: dict[str, float | None] | None = None
    calibration: dict[str, float | None] | None = None


@router.get("/api/admin/agentcore-rates")
def get_agentcore_rates(db: Session = Depends(get_db)) -> dict[str, Any]:
    return rates.agentcore_rates_view(db)


@router.put("/api/admin/agentcore-rates")
def put_agentcore_rates(
    body: AgentCoreRatesIn, request: Request, db: Session = Depends(get_db)
) -> dict[str, Any]:
    return rates.set_agentcore_rates(
        db, rates=body.rates, calibration_values=body.calibration,
        actor=require_identity(request).username,
    )


# ── my usage ─────────────────────────────────────────────────────────────────


@router.get("/api/me/usage")
def my_usage(
    request: Request, period: str | None = None, db: Session = Depends(get_db)
) -> dict[str, Any]:
    return budgets.usage_summary(db, budgets.payer_of_request(request), period)
