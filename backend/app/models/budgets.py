"""Per-user spend budgets: levels, the usage ledger and the session → payer map.

All three tables are **hub-global**, deliberately absent from
``WORKSPACE_SCOPED_TABLES``: a budget is one per person across every workspace,
and spend must outlive a deleted workspace. The workspace a charge came from is
recorded as ``origin_workspace_id`` — never ``workspace_id``, because any model
carrying a ``workspace_id`` column must be workspace-scoped
(``tests/test_workspaces.py``), and a scoped row would be purged with its
workspace, erasing the spend.

``users.budget_level_id`` / ``users.budget_override_usd`` (``models/ledger.py``)
carry the assignment; ``services/budgets.py`` is the only reader of these rows.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _id, _now


class BudgetLevel(Base):
    """A named USD budget per calendar month (UTC). At most one is the default:
    a member with no level of their own falls into it."""

    __tablename__ = "budget_levels"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    monthly_budget_usd: Mapped[float] = mapped_column(Float, default=0.0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str | None] = mapped_column(String(32), default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class UsageEvent(Base):
    """One metered or estimated charge. Append-only: a correction is a new row
    that the old one points at through ``superseded_by``; spend is the sum of the
    rows nothing superseded.

    ``user_id`` is NULL for an exempt principal with no ``users`` row (the config
    admin, the auth-disabled operator, a NULL-creator API key); ``user_name`` is
    the display name at charge time. ``usd`` is NULL when the model had no price —
    never a silent 0, so the admin price editor can list it.
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        Index("ix_usage_events_user_period", "user_id", "period"),
        Index("ix_usage_events_ref", "ref_type", "ref_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    user_name: Mapped[str | None] = mapped_column(String(64), default=None)
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM, UTC
    # chat | v1 | invoke | architect | eval | experiment | canary | skill_lab |
    # observability | codegen | … (services/budgets.SOURCES)
    source: Mapped[str] = mapped_column(String(32))
    # tokens | turn_estimate | evaluation_builtin | evaluation_custom (core);
    # job_estimate (jobs); runtime_compute, memory_event, … (reconcile)
    kind: Mapped[str] = mapped_column(String(32), default="tokens")
    origin_workspace_id: Mapped[str | None] = mapped_column(String(32), default=None)
    agent_id: Mapped[str | None] = mapped_column(String(32), default=None)
    session_id: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    model_id: Mapped[str | None] = mapped_column(String(160), default=None)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cache_read: Mapped[int] = mapped_column(Integer, default=0)
    cache_write: Mapped[int] = mapped_column(Integer, default=0)
    quantity: Mapped[float | None] = mapped_column(Float, default=None)
    unit: Mapped[str | None] = mapped_column(String(32), default=None)
    usd: Mapped[float | None] = mapped_column(Float, default=None)
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    ref_type: Mapped[str | None] = mapped_column(String(32), default=None)
    ref_id: Mapped[str | None] = mapped_column(String(128), default=None)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    superseded_by: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UsageSession(Base):
    """Which person pays for a billable AgentCore session (written by the meters,
    read by reconciliation). The first payer of a session keeps it: a continuing
    session is the same conversation, and its Runtime idle tail is that person's."""

    __tablename__ = "usage_sessions"

    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    origin_workspace_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    user_name: Mapped[str | None] = mapped_column(String(64), default=None)
    source: Mapped[str] = mapped_column(String(32))
    agent_id: Mapped[str | None] = mapped_column(String(32), default=None)
    resource_id: Mapped[str | None] = mapped_column(String(256), default=None)  # agent ARN
    first_turn_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_turn_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    turns: Mapped[int] = mapped_column(Integer, default=0)
    reconciled_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
