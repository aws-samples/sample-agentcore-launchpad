"""Platform ledger — every agent, deployment and background job lives here.

The ledger is the source of truth for what the platform created; AWS-side
resources are always reachable from a row (arn / resource id).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.project_scope import ProjectOwned, ProjectShared


def _id() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(UTC)


class Workspace(Base):
    """One (account, region) environment with its own AgentCore resource map.

    The row — not ``Settings`` — is authoritative for where work lands. The
    ``default`` row is seeded from settings at startup (see
    ``app.core.db._seed_default_workspace``) and its id is reserved.
    """

    __tablename__ = "workspaces"
    # One workspace per (account, region): every region-scoped resource name
    # Launchpad provisions (launchpad-gw, launchpad_memory, ...) stays
    # collision-free without a per-workspace name discriminator.
    __table_args__ = (
        UniqueConstraint("account_id", "region", name="uq_workspaces_account_region"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # slug
    name: Mapped[str] = mapped_column(String(64))
    account_id: Mapped[str] = mapped_column(String(16))
    region: Mapped[str] = mapped_column(String(32))
    # NULL means the hub's own ambient credentials (same-account workspace).
    role_arn: Mapped[str | None] = mapped_column(String(256), default=None)
    external_id: Mapped[str | None] = mapped_column(String(128), default=None)
    bootstrap_status: Mapped[str] = mapped_column(String(16), default="registered")
    # registered | bootstrapping | ready | failed
    # dev | staging | prod (T05). Ledger-authoritative for every row, `default`
    # included: the startup mirror never writes it. `prod` refuses member agent
    # mutations (route_policy.PROD_PROTECTED); server_default keeps the raw-SQL
    # seed of `default` (which does not name the column) on `dev`.
    tier: Mapped[str] = mapped_column(String(16), default="dev", server_default="dev")
    # T26: admin-configured release policy for promotions INTO this workspace (eval
    # threshold, ENFORCE requirement, deploy window, freezes). Empty = tier defaults.
    # Nullable: the raw-SQL seed of `default` does not name the column.
    release_policy: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    resources: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Operator-owned workspace policy blob (vs `resources`, which bootstrap owns).
    # Keys: "inbound_auth_default" — the InboundAuth agents inherit when their
    # spec carries none. Unlike `resources`, this is ledger-authoritative for
    # EVERY workspace including `default` (the startup mirror leaves it alone).
    settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class UserWorkspace(Base):
    """Binary grant: a member may operate in this workspace.

    Admins bypass grants entirely — the built-in admin is config-driven and has
    no ``users`` row, so it can never own one of these.
    """

    __tablename__ = "user_workspaces"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Agent(ProjectShared, Base):
    __tablename__ = "agents"
    # a new agent names its business project (services/agent_project); the stamp never
    # files one under a workspace `default` project silently
    __project_no_default_stamp__ = True

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # uniqueness among non-deleted rows is enforced in the API layer, so a
    # deleted agent's name can be reused
    name: Mapped[str] = mapped_column(String(64), index=True)
    method: Mapped[str] = mapped_column(String(24))  # harness|zip_runtime|container|studio|byoc
    status: Mapped[str] = mapped_column(String(24), default="draft")
    # draft | deploying | active | failed | deleted
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    resource_id: Mapped[str | None] = mapped_column(String(128), default=None)
    arn: Mapped[str | None] = mapped_column(String(256), default=None)
    registry_record_id: Mapped[str | None] = mapped_column(String(64), default=None)
    version: Mapped[str | None] = mapped_column(String(16), default=None)
    # Server-owned: the runtime version whose packaged entrypoint acknowledges
    # native attachments. Never copied from AgentSpec or an import descriptor.
    attachment_version: Mapped[str | None] = mapped_column(String(16), default=None)
    # Provenance label only ("river" default, "system", "aws-discovery", the
    # architect approver's username) — not a principal. Ownership is `created_by`.
    owner: Mapped[str] = mapped_column(String(64), default="river")
    # The creating console principal (`app.services.creators`): `users.id` — the
    # ownership key workspace visibility filters on — and the username at
    # creation (display). NULL for discovered runtimes, system presets and
    # legacy rows: a creator-unknown agent stays visible to every member.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # Server-owned system identity. NULL for every ordinary agent; a preset key
    # (``app.system_agents.presets``) for a platform-managed preset the console
    # installed on an administrator's explicit request. Never read from a client
    # payload — ``AgentSpec`` has no such field — and never editable through the
    # ordinary lifecycle routes, which refuse rows that carry it.
    system_key: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    # Which endpoint production traffic uses (docs/agent-dlc-design.md §6): "default"
    # (the auto-rolling DEFAULT, the historical behaviour) or "live" (a named endpoint
    # that only moves when a release is decided).
    endpoint_mode: Mapped[str] = mapped_column(
        String(16), default="default", server_default="default"
    )
    # Inbound auth actually deployed onto the Runtime — the RESOLVED choice
    # (spec > workspace default > iam), snapshotted by the deploy stage so the
    # invoke path and the console never re-resolve against a default that may
    # have changed since. "iam" | "jwt"; NULL for rows that predate the feature
    # (which all deployed without an authorizer, i.e. IAM).
    inbound_auth_mode: Mapped[str | None] = mapped_column(String(8), default=None)
    # The resolved InboundAuth dict when mode is jwt (discovery url, allowed
    # clients/audience/scopes, custom claims — no secrets live here).
    inbound_auth_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # Phase 2 isolation (projects design §10.2): the project permissions-boundary
    # version the agent's execution role carries now (`boundary-v<N>`), for drift
    # detection against the project's current boundary. NULL = no boundary attached
    # (a shared-mode project, or an isolated one not yet migrated).
    isolation_applied: Mapped[str | None] = mapped_column(String(16), default=None)
    # Set when a delete removed the console row but AWS still held the resource
    # (`agent.teardown_pending`, e.g. canary or release endpoints still DELETING):
    # `dlc.scheduler.sweep_deleted_resources` retries every such row, whatever its
    # endpoint_mode, and clears it once AWS has let go. NULL = nothing left to finish.
    teardown_pending_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    __table_args__ = (
        # One live preset per (workspace, key). Partial so a deleted (uninstalled)
        # preset does not block a later reinstall, and so ordinary rows (NULL key)
        # are never compared. Two concurrent installs race into this index; the
        # loser's INSERT fails and the install handler re-reads the winner.
        Index(
            "uq_agents_workspace_system_key",
            "workspace_id",
            "system_key",
            unique=True,
            sqlite_where=text("system_key IS NOT NULL AND status != 'deleted'"),
        ),
    )

    def project_platform_row(self) -> bool:
        """A system-managed preset is platform level: every project reads and invokes
        it, only administrators change it (design §4.2)."""
        return bool(self.system_key)


class SystemSkillRecord(Base):
    """Server-owned association between a system preset's published Skill bundle and
    the Agent Registry record that describes it (SE-043).

    One row per (workspace, preset) — the mapping outlives the preset agent (an
    uninstall keeps the record and the S3 release for other consumers) and a reinstall
    reuses it, so it is deliberately **not** ``Agent.registry_record_id`` (that column
    is the agent's own A2A record). Nothing here is derived from a client payload:
    the row is written only by the registration service, and a registry record is
    system-protected exactly when a row in the caller's workspace names its id.
    ``client_token`` is the durable CreateRegistryRecord intent: it is persisted
    before the AWS call so a crash between the call and the commit is recovered by
    repeating the same idempotent request, never by adopting a same-name record.
    AWS keeps the record's payload and approval status; the ledger holds identifiers
    and the release identity it registered.
    """

    __tablename__ = "system_skill_records"
    __table_args__ = (
        # One mapping per (workspace, preset); two racing first registrations hit this
        # index and the loser re-reads the winner's row. An index rather than a table
        # constraint so the pre-workspace migration rehearsal can drop the column.
        Index(
            "uq_system_skill_records_workspace_preset",
            "workspace_id",
            "preset_key",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    preset_key: Mapped[str] = mapped_column(String(64), index=True)
    skill_name: Mapped[str] = mapped_column(String(64))
    # the registry the record lives in — a workspace whose registry was rebuilt must
    # not be matched against a record id from the old one
    registry_id: Mapped[str] = mapped_column(String(128))
    # the VERIFIED record (read back and matched against the request that wrote it)
    record_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    record_arn: Mapped[str | None] = mapped_column(String(512), default=None)
    # the record id AWS returned for OUR create whose read-back has not verified yet;
    # protected like a verified one (it is ours), never projected as registered
    pending_record_id: Mapped[str | None] = mapped_column(String(64), default=None)
    client_token: Mapped[str] = mapped_column(String(256))
    # the COMPLETE CreateRegistryRecord kwargs persisted before the call: a retry
    # after a lost response replays exactly these bytes with the same token — the
    # only thing that proves a same-name record was created by this platform
    create_request: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # creating | accepted | registered — ledger-side lifecycle only; approval is AWS's
    status: Mapped[str] = mapped_column(String(16), default="creating")
    # the verified release (what the record is known to describe)
    release_version: Mapped[str | None] = mapped_column(String(32), default=None)
    release_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    s3_uri: Mapped[str | None] = mapped_column(String(512), default=None)
    content_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    # high-water mark: the release the last create/update INTENDED to write, committed
    # before the AWS call, so an accepted-but-lost update can never be downgraded by a
    # worker that still holds the previous release
    intent_version: Mapped[str | None] = mapped_column(String(32), default=None)
    intent_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    intent_content_digest: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ManagedMemory(ProjectShared, Base):
    """An AgentCore Memory this workspace manages (issue #55).

    The workspace's AWS account can hold memories the platform never created
    (another team's, another tool's), and the account boundary is not the
    workspace boundary: a memory is *managed* only when its id is the
    workspace's bootstrap memory (resource map) or a row here names it. Rows are
    written only by the server — when the console creates a memory, or when an
    administrator explicitly adopts an existing one — never from a client
    payload. Only a managed memory can be pinned by an agent spec, read back
    for an agent, or edited / deleted through ``/api/memory/resources``.
    AWS keeps the resource itself; the ledger holds the id and who brought it in.
    """

    __tablename__ = "managed_memories"
    __table_args__ = (
        Index("uq_managed_memories_workspace_memory", "workspace_id", "memory_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    memory_id: Mapped[str] = mapped_column(String(128), index=True)
    # created (console CreateMemory) | adopted (administrator adopted an existing one)
    origin: Mapped[str] = mapped_column(String(16), default="created")
    # Who brought it in (`app.services.creators`): `created_by` is the creator's
    # `users.id` — NULL for the config admin / auth-disabled operator, for an
    # adopted memory (it pre-existed in AWS, so the adopter is not its owner) and
    # for rows whose username had no `users` row at migration. `created_by_name`
    # is the creating (or adopting) principal's username, display only.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ResourceCreator(ProjectOwned, Base):
    """Who created an AWS-only resource through the console.

    Knowledge bases and custom evaluators have no ledger row of their own — AWS is
    their only store — so their creator lives here, keyed on (workspace, kind,
    AWS resource id). Written only by the server right after the console's create
    call succeeds and removed by the console's delete; a resource created outside
    the console has no row (its creator reads as unknown), and one deleted outside
    it leaves a harmless orphan that the workspace purge clears. Same column
    semantics as `ApiKey.created_by` / `created_by_name` (not a FK).
    """

    __tablename__ = "resource_creators"
    __table_args__ = (
        Index(
            "uq_resource_creators_workspace_kind_resource",
            "workspace_id",
            "kind",
            "resource_id",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # knowledge_base | evaluator
    resource_id: Mapped[str] = mapped_column(String(128))
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


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
    # Per-user agent-management permission overrides ({key: bool}). None and any
    # missing key mean GRANTED — only explicit denials are stored, so new
    # permission keys are default-on for every existing account. Inert for
    # admins (role short-circuits in auth). Keys: auth.AGENT_PERMISSIONS.
    permissions: Mapped[dict[str, bool] | None] = mapped_column(JSON, default=None)
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
    # Stamped once, on the first successful login (TTFA's start clock). NULL for
    # accounts that never logged in or that logged in before the column existed —
    # TTFA falls back to `created_at` for those.
    first_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # Forced logout (design §7.4): part of the session cookie's credential stamp, so a
    # bump ends every open session. 0 stamps exactly as before the column existed.
    session_epoch: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # SSO (sso design §2.2): the console identity provider and its `sub` this account
    # was created for. The pair is the account's key at sign-in — never the email, so
    # re-binding an address at the IdP cannot take over another account. NULL = local.
    external_idp_id: Mapped[str | None] = mapped_column(String(32), default=None)
    external_subject: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )

    __table_args__ = (
        Index("ix_users_external_identity", "external_idp_id", "external_subject",
              unique=True),
    )


class Deployment(ProjectShared, Base):
    __tablename__ = "deployments"
    __project_parent__ = ("agent_id", ("agents",))

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
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


class ChatSession(ProjectOwned, Base):
    __tablename__ = "chat_sessions"
    __project_parent__ = ("agent_id", ("agents",))

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    actor_id: Mapped[str] = mapped_column(String(64), default="river")
    turns: Mapped[int] = mapped_column(default=0)
    # summed from the runtime's `usage` events (projects §16.3-3); 0 = not reported
    input_tokens: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    total_tokens: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    runtime_version: Mapped[str | None] = mapped_column(String(16), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    # Set when the console explicitly ended the AgentCore Runtime session
    # (StopRuntimeSession). The row stays so the transcript remains replayable;
    # only new turns must go to a fresh session id.
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )


class ChatMessage(ProjectOwned, Base):
    """One rendered thread item of a console chat session (user turn, agent
    answer, tool call, error) — the Chat playground's reload-safe history.
    Integer autoincrement pk doubles as the replay order."""

    __tablename__ = "chat_messages"
    __project_parent__ = ("agent_id", ("agents",))

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # carried on the row itself, not only via the agent: two queries match on a
    # bare session_id (routers/chat.py, services/observability.py)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    role: Mapped[str] = mapped_column(String(16))  # user|agent|tool|auth|policy|error
    text: Mapped[str] = mapped_column(Text, default="")
    name: Mapped[str | None] = mapped_column(String(80), default=None)  # tool name
    attachments: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    # T35: `rule:<rule id>` when a curated answer (not the model) produced this row
    answered_by: Mapped[str | None] = mapped_column(String(48), default=None)
    # chat-streaming §4.1: on a retry turn's user row, the id of the failed turn's user
    # row it replaces — that turn stays in the ledger but is not replayed by /history
    retry_of: Mapped[int | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ApiKey(ProjectOwned, Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))  # display only, e.g. lp_live_ab12
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # T16 scope + limits. NULL/empty `agent_ids` = every agent in the workspace (the
    # behaviour of every key minted before scoping); NULL `expires_at` never expires;
    # NULL `rate_per_minute` is unlimited.
    agent_ids: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    rate_per_minute: Mapped[int | None] = mapped_column(default=None)
    # Usage stamps, written by /v1 auth on every admitted call (`record_use`, T16):
    # `use_count` is exact, `last_used_at` the latest admitted call.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    use_count: Mapped[int] = mapped_column(default=0, server_default="0")
    # The minting console principal: `created_by` is its `users.id`, NULL for the
    # config admin / auth-disabled operator (no `users` row) and for keys minted
    # before this column existed. Not a FK — a deleted creator must leave the id
    # behind, since that dangling id is how the key knows its creator is gone.
    # `created_by_name` is the display name at mint time (NULL only on legacy rows;
    # a key minted by the T16 line had its username in `created_by`, which the
    # migration moves here — `db._migrate_api_key_lifecycle_columns`).
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)


class ApiKeyUsage(ProjectOwned, Base):
    """Admitted `/v1` calls per key per UTC day (T16) — the aggregable usage shape."""

    __tablename__ = "api_key_usage"
    __project_parent__ = ("key_id", ("api_keys",))
    __table_args__ = (UniqueConstraint("key_id", "day", name="uq_api_key_usage_key_day"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    key_id: Mapped[str] = mapped_column(String(32), index=True)
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD, UTC
    count: Mapped[int] = mapped_column(default=0)


class SpecSnapshot(ProjectShared, Base):
    """The full agent spec as of one publish (T18): one row per create/redeploy.

    Rollback re-publishes a stored spec as a NEW snapshot; rows are never edited
    except to fill `aws_version` once the deploy stage learns it.
    """

    __tablename__ = "spec_snapshots"
    __project_parent__ = ("agent_id", ("agents",))
    __table_args__ = (UniqueConstraint("agent_id", "seq", name="uq_spec_snapshots_agent_seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    seq: Mapped[int]
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    aws_version: Mapped[str | None] = mapped_column(String(16), default=None)
    deployment_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ShareLink(ProjectOwned, Base):
    """A signed, revocable, account-free way for an outsider to reach exactly one
    thing (T13). The raw token is shown once at creation; only its sha256 is at
    rest, exactly like `ApiKey`. The row — not any request header — decides which
    workspace a visitor lands in."""

    __tablename__ = "share_links"
    __project_parent__ = ("target_id", ("agents", "annotation_tasks"))

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    kind: Mapped[str] = mapped_column(String(16), default="chat")  # chat (extensible)
    target_id: Mapped[str] = mapped_column(String(32), index=True)  # the agent id for `chat`
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256
    prefix: Mapped[str] = mapped_column(String(16), default="")  # display only, e.g. shr_ab12
    label: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    # `users.id` of the creator (design §5.8); NULL for the built-in admin and older rows
    created_by_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    enabled: Mapped[bool] = mapped_column(default=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    use_count: Mapped[int] = mapped_column(default=0)
    # Channel links (T30) only: `kind` is the platform (slack|feishu). `channel_config`
    # is what the console may show back (e.g. Feishu domain); `channel_secrets` is what
    # the adapter must be able to use (signing secret, bot token) and is NEVER
    # serialized by any route - the API reports only which keys are set.
    channel_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    channel_secrets: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChatFeedback(ProjectOwned, Base):
    """A thumbs verdict on one agent answer (T15), from the console or a share page.

    A table rather than columns on `ChatMessage`: one message can be rated by
    several actors, a verdict can change or be withdrawn, and it carries a comment
    and an origin — none of which belong on the transcript row. One verdict per
    (message, actor); re-voting updates it.
    """

    __tablename__ = "chat_feedback"
    __project_parent__ = ("agent_id", ("agents",))
    __table_args__ = (UniqueConstraint("message_id", "actor", name="uq_chat_feedback_actor"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    message_id: Mapped[int] = mapped_column(index=True)  # chat_messages.id (an agent answer)
    verdict: Mapped[str] = mapped_column(String(8), index=True)  # up | down
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    # T34: the answer a reviewer says it should have been (feeds a curated answer)
    correction: Mapped[str | None] = mapped_column(Text, default=None)
    actor: Mapped[str] = mapped_column(String(96))  # console username, or share:<link id>
    source: Mapped[str] = mapped_column(String(16), default="console")  # console|share|review
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class SharedTemplate(Base):
    """An agent published as a reusable starting point (roadmap T38).

    Hub-global on purpose: the whole point is that *another* workspace can start from it, so
    unlike every per-environment table this one is not workspace-scoped. The provenance
    column is deliberately NOT named `workspace_id`: in this ledger that name *means* "scoped
    to", and `test_workspaces` enforces exactly that — `source_workspace_id` records where it
    came from, for attribution and for deciding who may unpublish it.

    What the row carries is pruned by `services/marketplace.publishable_spec` — no
    environment-specific ids and nothing secret-shaped; those become `requirements` telling
    a consumer what to supply instead.
    """

    __tablename__ = "shared_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    # the publishing environment, NOT a scope — reads are deliberately cross-workspace
    source_workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    # the publishing project (design §4.2), provenance like the workspace above
    source_project_id: Mapped[str | None] = mapped_column(String(32), default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    source_agent_name: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(80), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    method: Mapped[str] = mapped_column(String(24), default="harness")
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    requirements: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    published_by: Mapped[str | None] = mapped_column(String(64), default=None)
    uses: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class AlertRule(ProjectShared, Base):
    """One threshold the platform watches, and what it last saw (roadmap T29).

    Deliberately a *rule*, not a subscription: the value is computed from data the
    platform already collects (spans for error rate and latency, online-evaluation
    results for quality, the price map for spend), so a rule adds a threshold and a
    destination rather than a new telemetry path.

    `state` and `last_value` are the evaluation's own memory. They exist so a firing rule
    notifies **once** on the transition rather than on every tick, and so the inbox can
    show what is wrong without re-running a billed Logs Insights scan.
    """

    __tablename__ = "alert_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # error_rate | latency_p95_ms | online_quality | cost_mtd_usd
    kind: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    # "above" | "below" — quality fires when it drops, the others when they rise
    comparison: Mapped[str] = mapped_column(String(8), default="above")
    threshold: Mapped[float] = mapped_column(Float)
    # the observability range key the value is measured over ("1h" … "30d")
    window: Mapped[str] = mapped_column(String(8), default="24h")
    enabled: Mapped[bool] = mapped_column(default=True)
    # Optional outbound destination. A generic JSON webhook, which is what a Slack or
    # Feishu incoming hook already is — so one mechanism covers both without the platform
    # holding channel credentials.
    webhook_url: Mapped[str | None] = mapped_column(String(512), default=None)
    # ok | firing | unknown (unknown = the last evaluation could not read the value)
    state: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    last_value: Mapped[float | None] = mapped_column(default=None)
    last_detail: Mapped[str | None] = mapped_column(Text, default=None)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ReleaseBundle(ProjectOwned, Base):
    """An immutable, verifiable record of exactly what was tested (T20).

    The unit of promotion: a frozen copy of one `SpecSnapshot`, the artifact
    coordinates that snapshot deployed (image digest / staged upload / requirements
    lock), the evaluation evidence pinned to it, and the policy posture at the time —
    reduced to one `digest`. Promotion deploys the bundle rather than re-generating
    from the spec, which is what makes "build once, deploy many" checkable: the same
    inputs always produce the same digest, and a digest mismatch means something other
    than the tested thing is about to ship.

    Rows are append-only. `workspace_id` is the environment the bundle was BUILT in
    (the source); where it may go is the promotion's business.
    """

    __tablename__ = "release_bundles"
    __project_parent__ = ("agent_id", ("agents",))

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    # the snapshot this bundle froze; null only if the snapshot was purged
    snapshot_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    snapshot_seq: Mapped[int | None] = mapped_column(default=None)
    agent_name: Mapped[str] = mapped_column(String(64))
    method: Mapped[str] = mapped_column(String(24))
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # {image_digest, image_uri, upload_id, requirements_sha, source_arn, aws_version}
    artifact: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # {run_id, dataset, dataset_version, pass_rate, evaluators, finished_at} or {}
    evaluation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # {guardrail, gateways, policy_engine_mode, registry_record_id}
    policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # sha256 over the canonical (spec, artifact, agent_name, method) — the identity
    digest: Mapped[str] = mapped_column(String(64), index=True)
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Promotion(ProjectOwned, Base):
    """One request to release a bundle into another environment (T21).

    The hand-off between the member who built an agent and the operator who runs it:
    a request carries the change note and the rollback plan, an approval records who
    accepted it and which gates passed at that moment, and execution (P3) attaches the
    job. `workspace_id` is the SOURCE environment, so the row is scoped like every
    other; `target_workspace_id` is where it is going.
    """

    __tablename__ = "promotions"
    __project_parent__ = ("bundle_id", ("release_bundles",))

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    bundle_id: Mapped[str] = mapped_column(ForeignKey("release_bundles.id"), index=True)
    target_workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    # pending | approved | rejected | executing | succeeded | failed | rolled_back
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    requested_by: Mapped[str | None] = mapped_column(String(64), default=None)
    # SoD (design §3.6): the immutable `users.id` beside each username, so a
    # re-registered username is never mistaken for the original requester
    requested_by_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    change_note: Mapped[str] = mapped_column(Text, default="")
    rollback_note: Mapped[str] = mapped_column(Text, default="")
    reviewed_by: Mapped[str | None] = mapped_column(String(64), default=None)
    reviewed_by_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    review_note: Mapped[str | None] = mapped_column(Text, default=None)
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # the gate results as evaluated at approval time — evidence, not a live read
    gates: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    job_id: Mapped[str | None] = mapped_column(String(32), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # T27 execution state. `stages` is the per-stage progress the console renders as one
    # card each; `execution` holds the resumable scratch (target agent, inner deploy job,
    # canary id, observation deadline) so a restarted worker continues instead of redoing.
    stages: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    execution: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # the bundle the target ran before this release: what a rollback re-deploys
    previous_bundle_id: Mapped[str | None] = mapped_column(String(32), default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class IdentityProvider(ProjectShared, Base):
    """Ledger row for a Connection: an AgentCore Identity credential provider
    created through Launchpad.

    Identifiers only — the client secret / API key goes straight to the token
    vault and is never stored or logged here. AWS stays the source of truth: the
    list endpoint reconciles against ``List*CredentialProviders`` and a row the
    vault no longer has surfaces as ``missing``. Same table (name, columns and
    unique index) the earlier identity fork shipped, plus the P1 columns
    ``client_id`` / ``scopes`` / ``template`` / ``description``.
    """

    __tablename__ = "identity_providers"
    __table_args__ = (
        # Provider names are unique per token vault = per (account, region) = per
        # workspace. An index rather than a table constraint so the workspace
        # migration rehearsal can drop workspace_id (as uq_system_skill_records_*).
        Index(
            "uq_identity_providers_ws_kind_name",
            "workspace_id",
            "kind",
            "name",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # oauth2 | api_key
    vendor: Mapped[str] = mapped_column(String(32), default="")  # CustomOauth2 | ... | ""
    arn: Mapped[str] = mapped_column(String(512), default="")
    # OAuth2 only: the redirect URI the operator registers at the IdP
    callback_url: Mapped[str | None] = mapped_column(String(1024), default=None)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # OAuth2 only: the (non-secret) client id, for display
    client_id: Mapped[str | None] = mapped_column(String(256), default=None)
    # OAuth2 only: the scope range the operator allows agents/targets to request
    scopes: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    # the console template the Connection was created from (e.g. "github")
    template: Mapped[str | None] = mapped_column(String(32), default=None)
    description: Mapped[str | None] = mapped_column(String(200), default=None)


class UserTokenRevocation(ProjectShared, Base):
    """A member's pending 3LO re-consent request for one Connection (identity P2).

    The service models no token-revocation operation, so "revoke" is deferred
    force-reauth: the next invoke for this (provider, user) passes
    ``forceAuthentication``. Claimed isomorphically from the earlier identity
    fork's ledgers so the table is not orphaned.

    Unlike the fork, the row stays in force until the user completes a NEW
    consent for the Connection (``oauth_sessions.complete``): every invoke in
    between sends ``forceAuthentication``, so a turn that happened not to call
    the tool cannot silently cancel the revocation.
    """

    __tablename__ = "user_token_revocations"
    __table_args__ = (
        Index(
            "uq_user_token_revocations_ws_provider_user",
            "workspace_id",
            "provider",
            "user_id",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    provider: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(String(128))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def project_platform_row(self) -> bool:
        """Per (user, Connection), not per project: one row, unique per workspace, that
        every project's invoke path honours (design §4.2) — always NULL."""
        return True


class OauthPendingSession(ProjectOwned, Base):
    """One in-flight 3LO consent session awaiting ``CompleteResourceTokenAuth``
    (identity P2). The user id comes from this row, never from the browser.
    Claimed isomorphically from the earlier identity fork's ledgers.
    """

    __tablename__ = "oauth_pending_sessions"
    __project_parent__ = ("agent_id", ("agents",))
    __table_args__ = (
        Index(
            "uq_oauth_pending_sessions_session_uri",
            "workspace_id",
            "session_uri",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    session_uri: Mapped[str] = mapped_column(String(512), index=True)
    provider: Mapped[str] = mapped_column(String(128))
    user_id: Mapped[str] = mapped_column(String(128))
    agent_id: Mapped[str] = mapped_column(String(32), default="")
    tool: Mapped[str] = mapped_column(String(80), default="")
    # Who the Runtime saw as the caller when it asked (identity P3): "iam"
    # (SigV4 + runtimeUserId), "user_jwt" (the user's own Cognito JWT) or
    # "m2m" (the workspace client_credentials token). Under a JWT authorizer
    # the vault keys the user on the inbound JWT, so the completion leg must
    # present a JWT of the same subject instead of the bare user id.
    caller_kind: Mapped[str] = mapped_column(String(16), default="iam")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class UserGrant(ProjectOwned, Base):
    """One user's as_user (3LO) grant state for one Connection on one agent.

    The token itself lives in the AgentCore token vault, keyed by (workload
    identity, user id, provider) — so a grant is per agent: each runtime has its
    own workload identity. The vault exposes no read of "is a token bound", so
    this row is derived progress the platform observes on its own legs:
    ``pending`` when an ``auth_required`` ask was forwarded, ``authorized`` when
    ``CompleteResourceTokenAuth`` succeeded, ``revoked`` when the user revoked
    the Connection (``user_token_revocations``). Token expiry stays with the
    vault: an expired binding simply asks again and the row goes ``pending``.
    """

    __tablename__ = "user_grants"
    __project_parent__ = ("agent_id", ("agents",))
    __table_args__ = (
        Index(
            "uq_user_grants_ws_user_provider_agent",
            "workspace_id",
            "user_id",
            "provider",
            "agent_id",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    user_id: Mapped[str] = mapped_column(String(128))
    provider: Mapped[str] = mapped_column(String(128))
    agent_id: Mapped[str] = mapped_column(String(32), default="")
    tool: Mapped[str] = mapped_column(String(80), default="")
    scopes: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    authorized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ContextLayer(Base):
    """The workspace's connected COA (Context Ontology Accelerator) deployment.

    One row per workspace (``services/context_layer.py``). Identifiers only:
    ``resources`` holds what discovery read from SSM + CloudFormation (runtime ARNs,
    the REST endpoint, the console URL, the COA user pool, issuer and agent OAuth
    client id). The client secret lives in the token vault behind ``connection``.
    Whether the runtimes are READY or the callback is registered is read live, never
    stored. Launchpad connects to COA; it never deploys or deletes it.
    """

    __tablename__ = "context_layers"
    __table_args__ = (
        # An index rather than a table constraint so the workspace migration
        # rehearsal can drop workspace_id (as uq_identity_providers_*).
        Index("uq_context_layers_workspace", "workspace_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    prefix: Mapped[str] = mapped_column(String(64))
    env: Mapped[str] = mapped_column(String(32), default="dev")
    # the COA deployment's region; may differ from the workspace's
    coa_region: Mapped[str] = mapped_column(String(32))
    resources: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # the OAuth2 Connection (credential provider name) holding the COA agent client
    connection: Mapped[str] = mapped_column(String(128))
    # the console workload identity the module and Harness agents fetch tokens with
    console_workload: Mapped[str] = mapped_column(String(255))
    # The connecting console principal (`app.services.creators`). Not a FK.
    created_by: Mapped[str | None] = mapped_column(String(32), default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class PolicyDecision(ProjectShared, Base):
    __tablename__ = "policy_decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    principal: Mapped[str] = mapped_column(String(96))  # e.g. demo@hr-analyst
    tool: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(8))  # ALLOW | DENY
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def project_platform_row(self) -> bool:
        """Governance of the shared gateway is workspace level (design §4.2): one
        decision log for every project."""
        return True


class PolicyChange(ProjectShared, Base):
    """Immutable request/snapshot fields plus mutable AWS operation outcome."""

    __tablename__ = "policy_changes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    # deliberately NOT in _POLICY_CHANGE_IMMUTABLE: the startup backfill must
    # stay legal on rows whose audit snapshot is otherwise frozen
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
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

    def project_platform_row(self) -> bool:
        """A change to the workspace-shared gateway is workspace level (design §4.2):
        every project reads the same audit trail and the same in-flight lock."""
        return True


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


class AuditEvent(ProjectShared, Base):
    """One journaled privileged action (T05): an administrator's break-glass agent
    mutation on a `prod` workspace, or a workspace tier change.

    Most rows are written at admission (before the handler runs), so `outcome`
    `success` records that the action was let through for that actor, not that the
    work it started succeeded — the Job log / Deployment row carries that. A request
    the platform turned away is `refused`, an action that ran and failed `failed`
    (design §11.6). Append-only by convention; nothing in the app updates or deletes a
    row except a workspace purge.
    """

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    actor: Mapped[str] = mapped_column(String(64))
    # `users.id` of the actor (design §5.10); NULL for the built-in admin, a system
    # actor and rows written before the column existed
    actor_user_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # e.g. "POST /api/agents/{agent_id}/redeploy" or "workspace.tier_change"
    action: Mapped[str] = mapped_column(String(160))
    # the concrete path, or "dev->prod" for a tier change
    target: Mapped[str] = mapped_column(String(256), default="")
    # success | refused | failed (design §11.6); rows before the column are success
    outcome: Mapped[str] = mapped_column(String(16), default="success",
                                         server_default="success")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# `storage_provision`: the console's S3 Files create / teardown is workspace
# infrastructure (services/storage_provision), like a bootstrap
_PLATFORM_JOB_TYPES = frozenset({"bootstrap_workspace", "uninstall_system_agent",
                                 "storage_provision"})


class Job(ProjectShared, Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    # the environment a resumed job re-runs against; never re-derived from settings
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
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

    def project_platform_row(self) -> bool:
        """Bootstrap and preset uninstall jobs serve the workspace, and a deploy of a
        system preset (`payload.platform`, set by create_deployment) serves the
        platform: none of them belongs to a project."""
        return self.type in _PLATFORM_JOB_TYPES or (self.payload or {}).get("platform") is True
