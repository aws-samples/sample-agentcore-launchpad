"""Skill Lab ledger models.

The ledger holds identifiers + display-derived counts only; task-set CONTENT
lives on disk under data/skill-lab/tasksets/<id>/ in skillopt-native files so
the vendored CLIs consume them untransformed (AWS-is-source-of-truth analogue:
here the artifact files are the source of truth)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _taskset_id() -> str:
    return "ts_" + uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(UTC)


class SkillLabTaskset(Base):
    __tablename__ = "skill_lab_tasksets"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_taskset_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(96))
    description: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(8))  # single | split
    # {"tasks": n} for single, {"train": n, "val": n, "test": n?} for split
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
