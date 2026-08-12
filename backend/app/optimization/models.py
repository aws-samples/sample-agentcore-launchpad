"""Experiment ledger — stage artifacts persist so the loop survives restarts."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

STAGES = [
    "recommend", "bundles", "gateway", "abtest", "traffic", "verdict",
    "promote", "cleanup",
]

RUNTIME_CANARY_STAGES = [
    "setup", "traffic", "verdict", "ramp", "complete", "rollback", "cleanup",
]


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(UTC)


class Experiment(Base):
    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(64))
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    agent_name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="running")
    # running | ready (verdict done) | promoted | cleaned | failed
    stage: Mapped[str] = mapped_column(String(16), default="recommend")
    # furthest stage the user has completed/unlocked (stepwise actions)
    artifacts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # per-stage results keyed by stage name (+ agent_meta snapshot)
    running_action: Mapped[str | None] = mapped_column(String(24), default=None)
    # stage action currently executing on a background thread, None when idle
    progress: Mapped[str | None] = mapped_column(Text, default=None)
    # free-form progress line for the UI while running_action is set
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class RuntimeCanary(Base):
    __tablename__ = "runtime_canaries"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(96))
    champion_agent_id: Mapped[str] = mapped_column(String(32), index=True)
    champion_agent_name: Mapped[str] = mapped_column(String(64))
    challenger_agent_id: Mapped[str] = mapped_column(String(32), index=True)
    challenger_agent_name: Mapped[str] = mapped_column(String(64))
    source_experiment_id: Mapped[str | None] = mapped_column(
        String(16), default=None, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="running")
    # running | completed | rolled_back | cleaned
    stage: Mapped[str] = mapped_column(String(16), default="setup")
    artifacts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    running_action: Mapped[str | None] = mapped_column(String(24), default=None)
    progress: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
