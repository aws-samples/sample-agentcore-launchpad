"""Evaluation ledger models (adapted from agentcore_eva_opt db.py row shapes)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(UTC)


class EvalDataset(Base):
    __tablename__ = "eval_datasets"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    # the `cloud` sync below targets one region, so the row belongs to one workspace
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="legacy")  # legacy|predefined
    locale: Mapped[str] = mapped_column(String(8), default="en")
    description: Mapped[str] = mapped_column(Text, default="")
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # legacy items: [{"prompt": str, "expected": str|None}]
    # predefined items (devguide scenario schema): [{"scenario_id", "turns":
    #   [{"input", "expected_response"?}], "expected_trajectory"?: [str],
    #   "assertions"?: [str], "metadata"?: {}}]
    cloud: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # cloud: {dataset_id, arn, status, synced_at, failure_reason} — last AWS sync
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    agent_name: Mapped[str] = mapped_column(String(64))
    dataset_id: Mapped[str | None] = mapped_column(String(16), default=None)
    dataset_name: Mapped[str | None] = mapped_column(String(64), default=None)
    mode: Mapped[str] = mapped_column(String(12), default="evaluators")  # evaluators|insights
    evaluators: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    # queued | invoking | waiting | evaluating | completed | failed
    queue_position: Mapped[int] = mapped_column(default=0)
    session_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    batch_eval_id: Mapped[str | None] = mapped_column(String(80), default=None)
    scores: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    insights: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
