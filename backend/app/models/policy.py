"""Self-hosted Policy ledger tables — the Global AgentCore Policy model (D20; policy-engines
contract ``docs/cn/interfaces/policy-engines.md``, 2026-10-28).

AgentCore Policy / Policy Engines do not exist in the China partition, so the model lives
here and the engine is self-hosted (Dogwood). The shape follows Global exactly:

* ``policy_engines`` — the PolicyEngine resource (many per workspace); carries the Dogwood
  set the hub last installed for it (``policy_set_seq`` → ``ps-<n>``, the temporal-set
  version and the install facts in ``engine_state``);
* ``policies`` — a policy belongs to ONE engine (``engine_id``) and has
  ``enforcement_mode`` ``active | log_only`` (Global ``ACTIVE | LOG_ONLY``); every edit is a
  new ``policy_versions`` row (statements are never rewritten), publishing a version copies
  it immutably to S3 (``policy/policies/<policy_id>/v<n>.cedar``) and records ``published_at``;
* ``policy_engine_attachments`` — Gateway.policyEngineConfiguration: at most one engine per
  gateway with ``mode`` ``LOG_ONLY | ENFORCE``; one engine may be attached to many gateways;
* ``policy_changes`` is the immutable operator journal and ``policy_decisions`` the
  per-decision audit (gateway and simulator rows; ``gateway_id`` on gateway rows).

Inert since the Global model (kept so an upgraded ledger opens; never read or written by the
API): ``policies.scope`` / ``policies.agent_ids`` (the retired agent scope) and the
``policy_settings`` row's workspace mode / switches / evidence columns (read ONCE by the
startup migration ``services/policy_engines.migrate``).

Every workspace-scoped table carries ``workspace_id`` and is listed in
``core.db.WORKSPACE_SCOPED_TABLES``; ``policy_engine_attachments`` and
``policy_gateway_attachments`` are hub-global, keyed by gateway (the engine carries the
workspace). The AWS account is not the workspace boundary.
"""

import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


def new_policy_id() -> str:
    return f"pol_{secrets.token_hex(6)}"


def new_engine_id() -> str:
    return f"pe_{secrets.token_hex(6)}"


def new_row_id() -> str:
    return secrets.token_hex(16)


POLICY_STATUSES = ("draft", "active", "retired")
ENGINE_STATUSES = ("active", "deleting", "deleted")
ENFORCEMENT_MODES = ("active", "log_only")  # a policy's; Global ACTIVE | LOG_ONLY
ATTACHMENT_MODES = ("LOG_ONLY", "ENFORCE")  # a gateway attachment's; Global's spelling
DECISIONS = ("allow", "deny", "would_deny", "error")
DECISION_SOURCES = ("gateway", "simulate", "runtime", "hub")  # the last two: historic rows
CHANGE_OPERATIONS = (
    "policy.create", "policy.update", "policy.publish", "policy.retire", "policy.delete",
    # the engine resource (Global PolicyEngine) and its attachments (Gateway.policyEngine…)
    "policy_engine.create", "policy_engine.update", "policy_engine.delete",
    "policy_engine.migrated",
    "gateway.attach", "gateway.detach",
    # the engine install that publish / retire runs before it commits (ok / failed)
    "engine.install",
    # the gateway bundle written after an install / attachment change (ok / failed)
    "gateway.bundle",
    # one policy's enforcement on the engine changed (active ↔ log_only)
    "policy.enforcement_mode",
    # historic journal rows (pre-Global-model ledgers): the deploy carrier snapshot and the
    # retired workspace mode / switches
    "bundle.apply", "mode.set",
)


class PolicyEngine(Base):
    """Global's PolicyEngine: a named container of policies (many per workspace), attached
    to gateways through ``PolicyEngineAttachment``. Holds the Dogwood set the hub last
    installed for it — set id on the engine = this row's ``id``, set version ``ps-<seq>``."""

    __tablename__ = "policy_engines"
    __table_args__ = (
        Index("uq_policy_engines_workspace_name", "workspace_id", "name", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_engine_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(48))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="active")  # active | deleting
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    # the last set the engine accepted (I5 install): `ps-<policy_set_seq>`, the temporal-set
    # version (bumped only when the temporal items change — T4 invalidation, the psid of
    # every new token) and the digest it was bumped for
    policy_set_seq: Mapped[int] = mapped_column(Integer, default=0)
    temporal_set_version: Mapped[int] = mapped_column(Integer, default=0)
    temporal_digest: Mapped[str] = mapped_column(String(64), default="")
    # the last SUCCESSFUL install's facts (counts, governed actions, set digest) — what every
    # attached gateway's bundle is composed from; `bundle_pending` while a bundle write after
    # it has not succeeded yet. NULL = never installed.
    engine_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Policy(Base):
    """One named Cedar policy of a policy engine (``engine_id``; ``workspace_id`` = the
    engine's). Statements live in ``PolicyVersion``; ``active_version`` / ``draft_version``
    point at the live and the editable one. Names are unique per engine."""

    __tablename__ = "policies"
    __table_args__ = (
        Index("uq_policies_engine_name", "engine_id", "name", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_policy_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    # NULL only on a row the startup migration has not visited yet (never through the API)
    engine_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(48))
    description: Mapped[str] = mapped_column(Text, default="")
    # INERT since the Global model (the retired agent scope): never read or written
    scope: Mapped[str] = mapped_column(String(16), default="workspace")
    agent_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft|active|retired
    # Global `enforcementMode`: `active` decides, `log_only` records what it would decide
    enforcement_mode: Mapped[str] = mapped_column(String(16), default="active")
    active_version: Mapped[int | None] = mapped_column(Integer, default=None)
    draft_version: Mapped[int | None] = mapped_column(Integer, default=None)
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class PolicyVersion(Base):
    """An immutable statement set. ``published_at`` + ``s3_key`` are set exactly once."""

    __tablename__ = "policy_versions"
    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_policy_versions_policy_version"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_row_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    policy_id: Mapped[str] = mapped_column(String(32), index=True)
    version: Mapped[int] = mapped_column(Integer)
    statement: Mapped[str] = mapped_column(Text)
    # stream A5: `stateless` | `temporal` (the compiler's classification on install) and the
    # statement language (`cedar` for D20 rows, `dogwood` once rewritten / authored anew)
    kind: Mapped[str] = mapped_column(String(16), default="stateless")
    dialect: Mapped[str] = mapped_column(String(16), default="cedar")
    schema_digest: Mapped[str] = mapped_column(String(64))
    validation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    authored_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    published_by: Mapped[str | None] = mapped_column(String(64), default=None)
    s3_key: Mapped[str | None] = mapped_column(String(256), default=None)


class PolicySettings(Base):
    """INERT since the Global model (2026-10-28): the retired per-workspace mode, switches,
    evidence window and engine-set state. Kept so an upgraded ledger opens and so the startup
    migration (``services/policy_engines.migrate``) can read the old posture ONCE; nothing
    reads or writes it afterwards. A fresh ledger never gets a row."""

    __tablename__ = "policy_settings"
    __table_args__ = (
        # One row per workspace. A named unique index (not `unique=True`) so the
        # pre-workspace upgrade path can drop it with the other `uq_*_workspace_*`
        # indexes (tests/test_workspaces.py) before re-adding the column.
        Index("uq_policy_settings_workspace", "workspace_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_row_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="log_only")  # off|log_only|enforce
    # Fixed to "closed" in v1 (plan §2.3(e)); the column exists so the posture is
    # auditable and a later release can make it a setting without a migration.
    invoke_fail_mode: Mapped[str] = mapped_column(String(16), default="closed")
    runtime_guard: Mapped[bool] = mapped_column(default=False)
    # Per-user principal + Gateway interceptor (contract policy-principal-contracts.md): the
    # workspace half of the switch; inert unless `Settings.policy_gateway_interceptor` too.
    gateway_interceptor: Mapped[bool] = mapped_column(default=False)
    # stream A5: whether a publish / retire installs this workspace's ACTIVE set on the
    # policy engine (compile → I5 install). Off by default and independent of the interceptor
    # half above: a workspace whose engine set is installed by hand (t1's M1 set ps-m1-1) keeps
    # it until an operator turns this on — a hub publish would replace it.
    engine_publish: Mapped[bool] = mapped_column(default=False)
    # stream A5: the last set the engine accepted (I5 install) — `ps-<policy_set_seq>`, the
    # temporal-set version (bumped only when the temporal items change: T4 invalidation, and
    # the psid of every new token) and the digest it was bumped for
    policy_set_seq: Mapped[int] = mapped_column(Integer, default=0)
    temporal_set_version: Mapped[int] = mapped_column(Integer, default=0)
    temporal_digest: Mapped[str] = mapped_column(String(64), default="")
    # stream A5a-2: this workspace's last SUCCESSFUL engine install — what its block of the
    # gateway bundle (I3 `workspaces.<ws>`) is composed from — plus `bundle_pending` while the
    # bundle write after it has not succeeded yet. NULL = never installed by the hub.
    engine_state: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    evidence_window: Mapped[str] = mapped_column(String(8), default="24h")
    updated_by: Mapped[str | None] = mapped_column(String(64), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class PolicyChange(Base):
    """Immutable operator journal (Global's ``PolicyChange`` minus the AWS fields)."""

    __tablename__ = "policy_changes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_row_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    # the engine the change belongs to (Global model); NULL on pre-migration rows
    engine_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    policy_id: Mapped[str | None] = mapped_column(String(32), default=None)
    version: Mapped[int | None] = mapped_column(Integer, default=None)
    operation: Mapped[str] = mapped_column(String(32))
    operator: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str] = mapped_column(String(16), default="succeeded")  # succeeded|failed
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    requested: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    override_reason: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )


class PolicyDecision(Base):
    """One authorization decision (hub invoke gate, runtime guard, or simulator)."""

    __tablename__ = "policy_decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_row_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), default=None)
    source: Mapped[str] = mapped_column(String(16), default="hub")  # hub|runtime|simulate|gateway
    channel: Mapped[str] = mapped_column(String(16), default="chat")  # chat|v1|system|runtime
    principal_kind: Mapped[str] = mapped_column(String(16))  # user|api_key|agent|system
    principal_id: Mapped[str] = mapped_column(String(64))
    principal_label: Mapped[str | None] = mapped_column(String(120), default=None)
    action: Mapped[str] = mapped_column(String(16))  # invoke|call_tool|list_tool
    resource_kind: Mapped[str] = mapped_column(String(16))  # agent|tool
    resource_id: Mapped[str] = mapped_column(String(160))
    agent_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    session_id: Mapped[str | None] = mapped_column(String(128), default=None)
    decision: Mapped[str] = mapped_column(String(16))  # allow|deny|would_deny|error
    mode: Mapped[str] = mapped_column(String(16))
    policy_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    policy_versions: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    bundle_version: Mapped[int | None] = mapped_column(Integer, default=None)
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    latency_ms: Mapped[float | None] = mapped_column(default=None)
    # the Gateway whose interceptor decided (source `gateway`: always set by the ingest; the
    # migration back-fills older rows from their workspace's gateway; NULL on other sources)
    gateway_id: Mapped[str | None] = mapped_column(String(128), index=True, default=None)
    # the engine whose set decided (the gateway's attachment at decision time; the simulator's
    # engine); NULL on historic rows the migration could not attribute
    engine_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # per-user policy stream B5 (source `gateway`; NULL on every other source)
    stage: Mapped[str | None] = mapped_column(String(8), default=None)  # invoke|a|bt|final
    session_psid: Mapped[str | None] = mapped_column(String(128), default=None)
    event_seq: Mapped[int | None] = mapped_column(Integer, default=None)
    store_epoch: Mapped[str | None] = mapped_column(String(32), default=None)
    input_keys: Mapped[list[str] | None] = mapped_column(JSON, default=None)  # names only
    # N17-obs482: when the hub's ingest wrote a `gateway` row (`created_at` is the interceptor's
    # own decision time), so the ingest lag is auditable after the fact; NULL on every other
    # source and on gateway rows ingested before the column existed. Set by the ingest path only.
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )


class PolicyEngineAttachment(Base):
    """Global's ``Gateway.policyEngineConfiguration``: the ONE engine a gateway's interceptor
    decides with and the gateway-level mode (``LOG_ONLY`` records, ``ENFORCE`` denies). No
    row = the gateway is unguarded (its interceptor is detached). Hub-global, keyed by
    gateway; the engine carries the workspace."""

    __tablename__ = "policy_engine_attachments"

    gateway_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    engine_id: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="LOG_ONLY")  # LOG_ONLY | ENFORCE
    attached_by: Mapped[str | None] = mapped_column(String(64), default=None)
    attached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class PolicyGatewayAttachment(Base):
    """Whether Launchpad's policy interceptor Lambda is attached to a gateway, as last
    VERIFIED by ``gateway_bootstrap.set_interceptor``'s read-back (attach or detach) —
    distinct from ``PolicyEngineAttachment`` (which engine, which mode): this is the
    AWS-side fact the attach / detach service proves. Hub-global, keyed by gateway; no row =
    not attached."""

    __tablename__ = "policy_gateway_attachments"

    gateway_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    interceptor_arn: Mapped[str | None] = mapped_column(String(256), default=None)
    attached: Mapped[bool] = mapped_column(default=False)
    attached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    detached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class PolicyRuntimeSession(Base):
    """Per-user policy stream A (re-review defect 2): with the principal carrier on, the
    AgentCore Runtime session a turn runs in is derived from (the caller's session id, the
    principal) — ``principal_mint.runtime_session_id`` — so two principals never share a
    runtime process (the token is held per process). This row remembers each derived id under
    the caller's session so the hub can still find it: ending a chat session stops every one,
    the Chat trace rail reads their spans. ``principal`` = ``<kind>:<id>``."""

    __tablename__ = "policy_runtime_sessions"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    principal: Mapped[str] = mapped_column(String(200), primary_key=True)
    runtime_session_id: Mapped[str] = mapped_column(String(128), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class EvalTokenFile(Base):
    """Per-user policy D5 (contract I9): one eval / canary job whose principal tokens the hub
    keeps in ``evaluations/<job_id>/tokens.json``. Holds what a refresh re-mints from — the
    principal CLAIM (who, never a token), the temporal-set version the psids were derived
    with and the session ids — so the refresh survives a hub restart (``reconcile_run``
    re-attaches to a worker that kept running). The row and the object go together:
    ``services/eval_tokens.discard`` deletes both at the job's terminal state."""

    __tablename__ = "eval_token_files"

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    owner_kind: Mapped[str] = mapped_column(String(16))  # eval_run | canary
    owner_id: Mapped[str] = mapped_column(String(64), index=True)
    agent_id: Mapped[str] = mapped_column(String(64))
    method: Mapped[str | None] = mapped_column(String(24), default=None)
    channel: Mapped[str] = mapped_column(String(16))
    principal: Mapped[dict[str, Any]] = mapped_column(JSON)
    tsv: Mapped[int] = mapped_column(Integer, default=0)
    session_ids: Mapped[list[str]] = mapped_column(JSON)
    written_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
