"""Hub-global platform settings an administrator changed on the console.

One row per overridden ``Settings`` field (today: the platform model roles of
``app.services.model_settings``). Precedence is console > env > launchpad.yaml >
default; a missing row means "not overridden here". Hub-global on purpose — not
in ``WORKSPACE_SCOPED_TABLES`` — and per ledger, so each deployment (each box with
its own SQLite file) carries its own overrides. Column names and types match the
console-v3 branch's table so a ledger written by either build reads in the other.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _now


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
