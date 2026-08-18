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


def _job_id() -> str:
    return "job_" + uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(UTC)


class SkillLabJob(Base):
    """One evaluation or training run of the vendored SkillOpt CLIs.

    The row is status + identifiers only: the CLI log streams from
    data/skill-lab/jobs/<id>/log.txt and results are re-read from out/ per
    request (studio parity). Shared by eval and train — `type` discriminates."""

    __tablename__ = "skill_lab_jobs"

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=_job_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    type: Mapped[str] = mapped_column(String(8))  # eval | train
    # queued | running | succeeded | failed | cancelled | interrupted
    status: Mapped[str] = mapped_column(String(12), default="queued")
    queue_position: Mapped[int] = mapped_column(default=0)
    # {kind: "registry"|"upload", record_id?, name, version?}
    skill_source: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    taskset_id: Mapped[str] = mapped_column(String(16), index=True)
    taskset_name: Mapped[str] = mapped_column(String(96), default="")
    split: Mapped[str] = mapped_column(String(8), default="")  # "" for single mode
    # {target_model, judge_model, workers, timeout, limit}
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


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
