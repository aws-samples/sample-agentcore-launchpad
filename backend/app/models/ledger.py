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


class Agent(Base):
    __tablename__ = "agents"

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
    # Inbound auth actually deployed onto the Runtime — the RESOLVED choice
    # (spec > workspace default > iam), snapshotted by the deploy stage so the
    # invoke path and the console never re-resolve against a default that may
    # have changed since. "iam" | "jwt"; NULL for rows that predate the feature
    # (which all deployed without an authorizer, i.e. IAM).
    inbound_auth_mode: Mapped[str | None] = mapped_column(String(8), default=None)
    # The resolved InboundAuth dict when mode is jwt (discovery url, allowed
    # clients/audience/scopes, custom claims — no secrets live here).
    inbound_auth_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
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


class ManagedMemory(Base):
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


class ResourceCreator(Base):
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
    # Spend budget assignment (services/budgets.py): a `budget_levels.id` (not a
    # FK — a level cannot be deleted while assigned, the service refuses it) and an
    # optional per-person amount that replaces the level's. Both NULL = the default
    # level if one is marked, else unlimited; admins stay exempt until assigned.
    budget_level_id: Mapped[str | None] = mapped_column(String(32), default=None)
    budget_override_usd: Mapped[float | None] = mapped_column(Float, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Deployment(Base):
    __tablename__ = "deployments"

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


class SpecSnapshot(Base):
    """The full agent spec of one publish (`app.services.snapshots`).

    `deployer.pipeline.create_deployment` writes one per create / redeploy / rollback /
    promotion; the canary paths that change `Agent.spec` without a deployment write
    their own, and a startup backfill gives an agent that predates the table a
    `baseline`. Rows are append-only except for the success stamp in `_finish`, which
    fills `aws_version` and replaces `spec` with the spec as deployed (a deploy stage
    may rewrite it mid-job). A rollback re-publishes a stored spec as a NEW row.

    The column set is a superset of the console-v3 branch's table of the same name
    (which lacks `created_by_name`, added by `_migrate_branch_creators`). There
    `created_by` held a username; here it is `users.id` (`app.services.creators`).
    """

    __tablename__ = "spec_snapshots"
    __table_args__ = (UniqueConstraint("agent_id", "seq", name="uq_spec_snapshots_agent_seq"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    seq: Mapped[int]  # 1, 2, … per agent
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # the AWS version the publish left live; NULL until the deploy succeeds (and for
    # good when it failed)
    aws_version: Mapped[str | None] = mapped_column(String(16), default=None)
    # NULL for the canary snapshots and the startup baseline
    deployment_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # Who published (`app.services.creators`): `users.id` and the username (display).
    # Not a FK. Falls back to the agent's creator when the path has no actor.
    created_by: Mapped[str | None] = mapped_column(String(32), default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)  # "rollback to #2", …
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    actor_id: Mapped[str] = mapped_column(String(64), default="river")
    turns: Mapped[int] = mapped_column(default=0)
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


class ChatMessage(Base):
    """One rendered thread item of a console chat session (user turn, agent
    answer, tool call, error) — the Chat playground's reload-safe history.
    Integer autoincrement pk doubles as the replay order."""

    __tablename__ = "chat_messages"

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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChatFeedback(Base):
    """One person's thumbs verdict on one agent answer of the console chat.

    A table rather than columns on `ChatMessage`: an answer can be rated by several
    people and a verdict can change or be withdrawn. One verdict per
    (message, actor); re-voting updates it.

    The column names and types are a **subset of the console-v3 branch's** table of
    the same name (which also has `correction`): a ledger that ran that branch
    already holds the table, `create_all` skips it, and `schema_drift` only reports
    model columns missing from it. A column added here needs a `_migrate*` ALTER.
    """

    __tablename__ = "chat_feedback"
    __table_args__ = (UniqueConstraint("message_id", "actor", name="uq_chat_feedback_actor"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)  # not a FK
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    message_id: Mapped[int] = mapped_column(index=True)  # chat_messages.id (an agent answer)
    verdict: Mapped[str] = mapped_column(String(8), index=True)  # up | down
    # never written by the console (no free-text comments yet); kept for v3 parity
    comment: Mapped[str | None] = mapped_column(Text, default=None)
    actor: Mapped[str] = mapped_column(String(96))  # console username
    # `console` here; a v3-era row may say `share` / `review`
    source: Mapped[str] = mapped_column(String(16), default="console")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))  # display only, e.g. lp_live_ab12
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # The minting console principal: `created_by` is its `users.id`, NULL for the
    # config admin / auth-disabled operator (no `users` row) and for keys minted
    # before this column existed. Not a FK — a deleted creator must leave the id
    # behind, since that dangling id is how the key knows its creator is gone.
    # `created_by_name` is the display name at mint time (NULL only on legacy rows).
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    # Stamped by /v1 auth on success, at most once per `LAST_USED_RESOLUTION`.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class IdentityProvider(Base):
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


class UserTokenRevocation(Base):
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


class OauthPendingSession(Base):
    """One in-flight 3LO consent session awaiting ``CompleteResourceTokenAuth``
    (identity P2). The user id comes from this row, never from the browser.
    Claimed isomorphically from the earlier identity fork's ledgers.
    """

    __tablename__ = "oauth_pending_sessions"
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


class UserGrant(Base):
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


class PolicyDecision(Base):
    __tablename__ = "policy_decisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    principal: Mapped[str] = mapped_column(String(96))  # e.g. demo@hr-analyst
    tool: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(8))  # ALLOW | DENY
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class PolicyChange(Base):
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


class AlertRule(Base):
    """One threshold the platform watches, and what it last saw.

    Deliberately a *rule*, not a subscription: the value is computed from data the
    platform already collects (spans for error rate and latency, online-evaluation
    results for quality, the price map for spend), so a rule adds a threshold and a
    destination rather than a new telemetry path (`app.services.alerts`).

    `state` and `last_value` are the evaluation's own memory: a firing rule notifies
    **once** per firing episode, rather than on every pass.

    The column set is a superset of the console-v3 branch's table of the same name
    (which lacks `created_by_name`, added by `_migrate_branch_creators`). There
    `created_by` held a username; here it is `users.id` (`app.services.creators`).
    """

    __tablename__ = "alert_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # error_rate | latency_p95_ms | online_quality | spend_usd | cost_mtd_usd
    kind: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    # "above" | "below" — quality fires when it drops, the others when they rise
    comparison: Mapped[str] = mapped_column(String(8), default="above")
    threshold: Mapped[float] = mapped_column(Float)
    # the range key the value is measured over ("1h" … "30d"); see services/alerts
    window: Mapped[str] = mapped_column(String(8), default="24h")
    enabled: Mapped[bool] = mapped_column(default=True)
    # Optional outbound destination: one generic https JSON webhook (a Slack or
    # Feishu incoming hook already is one). Never returned by the API.
    webhook_url: Mapped[str | None] = mapped_column(String(512), default=None)
    # ok | firing | unknown (unknown = the last evaluation could not read the value)
    state: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    last_value: Mapped[float | None] = mapped_column(default=None)
    last_detail: Mapped[str | None] = mapped_column(Text, default=None)
    last_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # when the rule last STARTED firing (the current/last episode), not last seen firing
    last_fired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # last delivery attempt; older than last_fired_at = this episode's post is still owed
    last_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # The creating console principal (`app.services.creators`): `users.id` and the
    # username at creation (display). Not a FK.
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
