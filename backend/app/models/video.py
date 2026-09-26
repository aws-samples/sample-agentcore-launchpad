"""Hub-global video drafts and published snapshots."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _id, _now


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_id)
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    published_content: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64))
    updated_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}


class VideoCatalogSeed(Base):
    """One-way marker: an intentionally emptied directory must stay empty on restart."""

    __tablename__ = "video_catalog_seed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seeded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
