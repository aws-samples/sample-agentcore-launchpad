"""Platform ledger — every agent, deployment and background job lives here.

The ledger is the source of truth for what the platform created; AWS-side
resources are always reachable from a row (arn / resource id).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, event, inspect
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    # uniqueness among non-deleted rows is enforced in the API layer, so a
    # deleted agent's name can be reused
    name: Mapped[str] = mapped_column(String(64), index=True)
    method: Mapped[str] = mapped_column(String(24))  # harness|zip_runtime|container|studio
    status: Mapped[str] = mapped_column(String(24), default="draft")
    # draft | deploying | active | failed | deleted
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    resource_id: Mapped[str | None] = mapped_column(String(128), default=None)
    arn: Mapped[str | None] = mapped_column(String(256), default=None)
    registry_record_id: Mapped[str | None] = mapped_column(String(64), default=None)
    version: Mapped[str | None] = mapped_column(String(16), default=None)
    owner: Mapped[str] = mapped_column(String(64), default="river")
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class User(Base):
    """A console account created by self-service registration (or by an admin).

    The built-in admin is **not** stored here — it stays config-driven
    (`auth_username`/`auth_password`) so a bad row can never lock the console.
    Registered accounts are time-boxed: `expires_at` is checked on every guarded
    request, so expiry/disable takes effect without waiting for the session
    cookie to lapse.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    username: Mapped[str] = mapped_column(String(64))  # as typed, for display
    # lowercase mirror; carries the uniqueness constraint so logins and
    # registration are case-insensitive on both username and email
    username_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(160), unique=True, index=True)  # lowercased
    password_hash: Mapped[str] = mapped_column(String(256))
    role: Mapped[str] = mapped_column(String(16), default="member")  # member | admin
    # pending accounts await admin approval and cannot hold a session; the
    # validity window below only starts once they are approved
    status: Mapped[str] = mapped_column(String(16), default="active")
    # pending | active | disabled
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )  # None = never expires (admin-granted)
    created_by: Mapped[str] = mapped_column(String(64), default="self")
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    login_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(String(32), default=None)
    status: Mapped[str] = mapped_column(String(24), default="running")
    # running | succeeded | failed
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # [{name, status: pending|running|succeeded|skipped|failed, detail, started_at, ended_at}]
    # Immutable ECR digest of the container image this deployment runs. Recorded so
    # the console can say exactly what is deployed and a resumed job re-uses the
    # same image rather than whatever the mutable tag points at by then. None for
    # zip/harness methods and for container deployments predating digest pinning.
    image_digest: Mapped[str | None] = mapped_column(String(80), default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    actor_id: Mapped[str] = mapped_column(String(64), default="river")
    turns: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ChatMessage(Base):
    """One rendered thread item of a console chat session (user turn, agent
    answer, tool call, error) — the Chat playground's reload-safe history.
    Integer autoincrement pk doubles as the replay order."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user | agent | tool | error
    text: Mapped[str] = mapped_column(Text, default="")
    name: Mapped[str | None] = mapped_column(String(80), default=None)  # tool name
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))  # display only, e.g. lp_live_ab12
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PolicyDecision(Base):
    __tablename__ = "policy_decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    principal: Mapped[str] = mapped_column(String(96))  # e.g. demo@hr-analyst
    tool: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(8))  # ALLOW | DENY
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PolicyChange(Base):
    """Immutable request/snapshot fields plus mutable AWS operation outcome."""

    __tablename__ = "policy_changes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    gateway_id: Mapped[str] = mapped_column(String(128), index=True)
    gateway_arn: Mapped[str] = mapped_column(String(512))
    gateway_name: Mapped[str] = mapped_column(String(100))
    engine_id: Mapped[str | None] = mapped_column(String(128), default=None)
    engine_arn: Mapped[str | None] = mapped_column(String(512), default=None)
    policy_id: Mapped[str | None] = mapped_column(String(128), default=None)
    policy_name: Mapped[str | None] = mapped_column(String(100), default=None)
    candidate_policy_id: Mapped[str | None] = mapped_column(String(128), default=None)
    operation: Mapped[str] = mapped_column(String(48))
    operator: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    before: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    requested: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    expected_updated_at: Mapped[str | None] = mapped_column(String(64), default=None)
    override_reason: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


_POLICY_CHANGE_IMMUTABLE = {
    "gateway_id",
    "gateway_arn",
    "gateway_name",
    "engine_id",
    "engine_arn",
    "policy_id",
    "policy_name",
    "operation",
    "operator",
    "before",
    "requested",
    "expected_updated_at",
    "override_reason",
    "created_at",
}


@event.listens_for(PolicyChange, "before_update")
def _prevent_policy_change_snapshot_mutation(_: Any, __: Any, target: PolicyChange) -> None:
    state = inspect(target)
    changed = [
        name for name in _POLICY_CHANGE_IMMUTABLE if state.attrs[name].history.has_changes()
    ]
    if changed:
        raise ValueError(f"immutable policy audit fields changed: {', '.join(sorted(changed))}")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    type: Mapped[str] = mapped_column(String(32))  # deploy_agent | delete_agent | ...
    status: Mapped[str] = mapped_column(String(16), default="queued")
    # queued | running | succeeded | failed
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    log: Mapped[str] = mapped_column(Text, default="")  # JSONL, one event per line
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
