"""Optimization ledger — Runtime canaries (stage artifacts persist so the loop survives
restarts), the config bundles the optimizer writes, and the sticky arm assignments."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

RUNTIME_CANARY_STAGES = [
    "setup", "traffic", "verdict", "ramp", "complete", "rollback", "cleanup",
]


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(UTC)


class Experiment(Base):
    """LEGACY — the configuration-bundle experiment (recommend → … → promote → cleanup).

    Retired by user decision (N13-obs2, 2026-10-17): the ``/api/experiments`` routes, the
    stage machine and the console surfaces are gone and ``routing`` never reads these rows.
    The table and this model stay only so an existing ledger keeps loading (no migration);
    ``main.create_app`` logs how many rows remain. Dropped in a later round.
    """

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
    # The creating console principal (`app.services.creators`): `users.id` (NULL for
    # the config admin / auth-disabled operator, system paths and legacy rows) and
    # the username at creation, display only. Server-set, never from a payload.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


#: `RuntimeCanary.status` values under which the canary still routes traffic to (or holds
#: versions of) its champion / challenger: `DELETE /api/agents/{id}` answers
#: 409 `agent.in_use` for either agent while one exists (N16-obs111). `completed`,
#: `rolled_back` and `cleaned` are terminal and never block.
CANARY_BLOCKING_STATUSES: frozenset[str] = frozenset({"running"})


class RuntimeCanary(Base):
    __tablename__ = "runtime_canaries"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(96))
    champion_agent_id: Mapped[str] = mapped_column(String(32), index=True)
    champion_agent_name: Mapped[str] = mapped_column(String(64))
    challenger_agent_id: Mapped[str] = mapped_column(String(32), index=True)
    challenger_agent_name: Mapped[str] = mapped_column(String(64))
    # LEGACY (N13-obs2): the retired experiment → canary promote hand-off; never set any
    # more and not projected by the API. Kept so an existing ledger loads (no migration).
    source_experiment_id: Mapped[str | None] = mapped_column(
        String(16), default=None, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="running")
    # running | completed | rolled_back | cleaned (CANARY_BLOCKING_STATUSES names the
    # non-terminal ones — an agent they route to cannot be deleted, N16-obs111)
    stage: Mapped[str] = mapped_column(String(16), default="setup")
    artifacts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    running_action: Mapped[str | None] = mapped_column(String(24), default=None)
    progress: Mapped[str | None] = mapped_column(Text, default=None)
    # N17-obs447: the running line's stable ``{code, params}`` (``progress_steps``) — the
    # console words it; NULL for a plain-text line or no action running
    progress_detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # The creating console principal (`app.services.creators`): `users.id` (NULL for
    # the config admin / auth-disabled operator, system paths and legacy rows) and
    # the username at creation, display only. Server-set, never from a payload.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


# ── CN additive tables (N12 E0-4; plan §A.2, contract amendment CA-3) ────────────


def _bundle_id() -> str:
    return "bndl_" + uuid.uuid4().hex[:12]


class ConfigBundle(Base):
    """An immutable configuration bundle version (the Global ``ConfigurationBundle``,
    locally): ``components.configuration = {system_prompt, tools: {name: {description}}}``
    mirrored to ``s3://<artifacts>/bundles/<agent_id>/<id>/v<version>.json``, which the
    runtime's ``_config_bundle()`` reads through the request-baggage pointer (CA-2).
    One row per (id, version); W-E2 writes, W-E3 points arms at them."""

    __tablename__ = "config_bundles"

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=_bundle_id)
    version: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64))
    components: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    s3_key: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ExperimentAssignment(Base):
    """Which arm a session belongs to — the backend's routing truth (CA-2 ruling).

    Written before a replay job is dispatched (the worker only executes what it is
    told) and by the live invoke chain's sticky routing; read by the verdict."""

    __tablename__ = "experiment_assignments"
    __table_args__ = (
        Index(
            "uq_experiment_assignments_owner_session", "owner_id", "session_id", unique=True
        ),
    )

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    owner_kind: Mapped[str] = mapped_column(String(12))  # canary (legacy rows: experiment)
    owner_id: Mapped[str] = mapped_column(String(16), index=True)
    session_id: Mapped[str] = mapped_column(String(256))
    arm: Mapped[str] = mapped_column(String(4))  # C|T1
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
