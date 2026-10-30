"""Platform ledger — every agent, deployment and background job lives here.

The ledger is the source of truth for what the platform created; AWS-side
resources are always reachable from a row (arn / resource id).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
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
    # Unused in this build (inbound auth is AWS_IAM-only — GRAPH-LOOP-SPEC N3 /
    # D9 — so there is no per-workspace default left to store here); kept for a
    # future operator-owned workspace policy. Unlike `resources`, this is
    # ledger-authoritative for EVERY workspace including `default` (the startup
    # mirror leaves it alone).
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
    # The creating console principal (`app.services.creators`, upstream #263 shape):
    # `created_by` is its `users.id` — the ownership key the creator-or-admin gate on
    # redeploy / delete checks — NULL for the config admin / auth-disabled operator, for
    # a system preset and a discovered runtime (no human creator), and for rows whose
    # pre-#261 `owner` username had no `users` row at migration. `created_by_name` is
    # the username at creation, display only. Server-set, never from `AgentSpec`.
    # (`owner`, the pre-#261 username column with its "river" default, was retired —
    # C5 — and is dropped by `core.db._migrate_creator_columns` after the copy.)
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # Server-owned system identity. NULL for every ordinary agent; a preset key
    # (``app.system_agents.presets``) for a platform-managed preset the console
    # installed on an administrator's explicit request. Never read from a client
    # payload — ``AgentSpec`` has no such field — and never editable through the
    # ordinary lifecycle routes, which refuse rows that carry it.
    system_key: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    # N17-obs202: on a system preset row only — True when the administrator pinned the
    # model through the preset's settings edit (an explicit `model_id`), False when the
    # stored `spec.model_id` is the `agent_default` model role as it stood at the last
    # install / repair (the row FOLLOWS the model-defaults page: a changed role re-publishes
    # it on the next repair). NULL = a row written before the flag: pinned iff its model
    # differs from the then-only default (`system_agents.service.model_pinned`).
    preset_model_pinned: Mapped[bool | None] = mapped_column(Boolean, default=None)
    # N17-obs31 (= N16-obs34): the forced delete of a knowledge base the spec mounted stripped
    # the pin here — `{kb_ids: [...], at: iso, by: username}` — and the deployed Runtime keeps
    # its retrieval tool until the next successful publish, which clears this (`_finish`).
    # The projection reads it as `kb_stale`; never a snapshot (a strip is not a publish).
    last_strip: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # Inbound auth actually deployed onto the Runtime — snapshotted by the
    # deploy stage. AWS_IAM-only in this build (GRAPH-LOOP-SPEC N3 / D9): every
    # new deploy writes "iam"; a pre-N3 row may still carry a stale "jwt" value
    # from before this build removed the JWT authorizer (`parse_inbound_auth`
    # reads it back as IAM-equivalent, never as a mode this build can serve).
    # NULL for rows that predate the feature entirely (also IAM).
    inbound_auth_mode: Mapped[str | None] = mapped_column(String(8), default=None)
    # Always NULL in this build (there is no JWT config left to snapshot); a
    # pre-N3 row may still carry a stale JWT config blob (discovery url,
    # allowed clients/audience/scopes, custom claims — no secrets live here),
    # which nothing in this build reads back.
    inbound_auth_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # D20 (P1-b): snapshot of the deploy-time policy narrowing the live version carries:
    # {stamp, mode, applied, patterns[], removed[], unenforceable[], gateway_dropped,
    # coverage, policy_versions{}, applied_at, job_id}. NULL before the first deploy under
    # the policy module. `policy_stale` is DERIVED from it on read
    # (services/policy_enforcement.is_stale) — never stored.
    policy_applied: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
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

    One row per (workspace, preset) — the mapping is independent of the agent row (a
    repair keeps it; the preset uninstall's ``registry`` step deletes record and mapping
    together, N10v3-bug6, while the S3 release stays for other consumers) and a reinstall
    registers afresh, so it is deliberately **not** ``Agent.registry_record_id`` (that
    column is the agent's own A2A record). Nothing here is derived from a client payload:
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


class RegistryRecord(Base):
    """One local Agent Registry record (N4). The CN partition has no Agent Registry
    service, so the ledger *is* the registry here — the one place the "AWS is the source
    of truth" rule inverts, acceptable because a record has no runtime state.

    ``record_type`` holds the platform descriptor type (``A2A`` / ``MCP`` /
    ``AGENT_SKILLS`` / ``CUSTOM``); ``descriptors`` is the platform descriptor tree the
    builders in ``services/registry_backend.py`` produce, stored as-is. ``search_text``
    is the flattened descriptor text FTS indexes (tool names, SKILL.md body, card skills);
    ``services/registry_local.py`` derives it on every write.
    """

    __tablename__ = "registry_records"
    __table_args__ = (
        # GA uniqueness, kept: one (name, recordVersion) per workspace. A unique INDEX
        # rather than a table constraint so `tests/test_ledger_migration.py` can drop the
        # `workspace_id` column in its upgrade rehearsal (SQLite cannot drop a column that
        # a table constraint names).
        Index(
            "uq_registry_records_ws_name_version",
            "workspace_id",
            "name",
            "record_version",
            unique=True,
        ),
        Index("ix_registry_records_ws_type_status", "workspace_id", "record_type", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    record_type: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64), index=True)
    description: Mapped[str] = mapped_column(String(500), default="")
    record_version: Mapped[str] = mapped_column(String(32))
    # DRAFT | PENDING_APPROVAL | APPROVED | REJECTED | DEPRECATED
    status: Mapped[str] = mapped_column(String(24), default="DRAFT")
    status_reason: Mapped[str | None] = mapped_column(String(500), default=None)
    descriptors: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tags: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    search_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    decided_by: Mapped[str | None] = mapped_column(String(96), default=None)
    # Who registered the record (`app.services.creators`, N15-obs7): `created_by` is the
    # creating principal's `users.id` — NULL for the config admin / auth-disabled operator,
    # deploy-created A2A records, gateway imports, platform defaults and legacy rows (an
    # administrator's, as everywhere) — and `created_by_name` the username, display only.
    # Reads stay shared (users-and-ownership.md §7); PUT / reimport / DELETE are
    # creator-or-admin (§8).
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)


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
    # adopted memory (it pre-existed, so the adopter is not its owner) and for rows
    # whose pre-#261 username had no `users` row at migration. `created_by_name` is
    # the creating (or adopting) principal's username, display only.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ResourceCreator(Base):
    """Who created an AWS-only resource through the console (upstream #261).

    A resource with no ledger row of its own keeps its creator here, keyed on
    (workspace, kind, resource id). Written only by the server right after the
    console's create call succeeds and removed by the console's delete; a resource
    created outside the console has no row (its creator reads as unknown), and one
    deleted outside it leaves a harmless orphan that the workspace purge clears.
    Same column semantics as `ApiKey.created_by` / `created_by_name` (not a FK).
    In the CN build every such kind (knowledge bases, evaluators) is ledger-backed, so
    the table holds no rows today — kept with upstream's API for parity (`services/
    creators.py`).
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


# --- CN memory/KB (N6) ---
class MemoryStore(Base):
    """A self-hosted memory store (N6): one row per store the console created or
    materialized by editing the workspace's default (`launchpad_memory-<workspace>`),
    which otherwise exists implicitly (ruling: default store synthesized). Data lives
    in S3 (`memory/<memory_id>/`) and OpenSearch (`store_id` filter); this row holds
    the console-facing identifiers and settings only."""

    __tablename__ = "memory_stores"
    __table_args__ = (
        Index("uq_memory_stores_workspace_memory", "workspace_id", "memory_id", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    memory_id: Mapped[str] = mapped_column(String(128), index=True)
    name: Mapped[str] = mapped_column(String(48))
    description: Mapped[str] = mapped_column(String(4096), default="")
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    event_expiry_days: Mapped[int] = mapped_column(default=30)
    strategies: Mapped[list[str]] = mapped_column(JSON, default=list)
    namespace_keys: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # r14-a (N14-obs1): merge strategy v2 knobs — recall half-life, per-actor caps, soft
    # TTL, rolling-summary interval (`opensearch_http.MERGE_SETTINGS_DEFAULTS`); NULL /
    # missing keys read as the defaults, so a pre-v2 row behaves as before.
    merge_settings: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # The creating console principal (`app.services.creators`): `users.id` (NULL for the
    # config admin / auth-disabled operator, the materialized default store and legacy
    # rows) + the username at creation, display only. Mirrors the `managed_memories` row
    # the console registers for the same store.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class KnowledgeBase(Base):
    """A self-hosted knowledge base (N6): Bedrock Knowledge Bases do not exist in
    `cn-northwest-1`, so this row is the console-facing identity of a KB whose
    documents live in S3 (`kb/<kb_id>/<ds_id>/`) and whose indexed chunks live in a
    dedicated OpenSearch index (`opensearch_http.kb_index_name(kb_id)`, one index per
    KB — ruling R3). `data_sources` holds the full data-source list inline (there is
    no AWS DataSource resource to mirror); ingestion job state lives in OpenSearch
    (ruling R4), never here."""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        Index("uq_knowledge_bases_workspace_name", "workspace_id", "name", unique=True),
    )

    kb_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(1000), default="")
    status: Mapped[str] = mapped_column(String(16), default="CREATING")
    # CREATING | ACTIVE | FAILED | DELETING
    failure_reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    # [{ds_id, name, mode, bucket, prefix, status, created_at}]
    data_sources: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # The creating console principal (`app.services.creators`): `users.id` (NULL for the
    # config admin / auth-disabled operator, preset-installed KBs and legacy rows) + the
    # username at creation, display only. Server-set, never from a payload.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
# --- end CN memory/KB (N6) ---


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
    # r12-g (rev-r11 ruling): bumped on every password change / reset and carried in the
    # session cookie, so a new password signs the account's other sessions out at once.
    session_epoch: Mapped[int] = mapped_column(default=0, server_default="0")
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
    # [{name, status: pending|running|succeeded|skipped|failed|cancelled|interrupted, detail,
    #   started_at, ended_at}] — `interrupted` (N17-obs113): the stage's subprocess was
    #   stopped with the service; the job stays `running` and resumes from that stage
    # Immutable ECR digest of the container image this deployment runs. Recorded so
    # the console can say exactly what is deployed and a resumed job re-uses the
    # same image rather than whatever the mutable tag points at by then. None for
    # zip/harness methods and for container deployments predating digest pinning.
    image_digest: Mapped[str | None] = mapped_column(String(80), default=None)
    # D21 (M1-d): the model-registry catalog version the `generate` stage rendered from and
    # the resolved descriptor of `spec.model_id` at that moment (`schemas.agent.
    # model_descriptor`; None for an id outside the catalog). The agent view compares the
    # stored descriptor with today's to raise the Redeploy hint — a new unrelated model does
    # not. None on rows that predate the column.
    catalog_version: Mapped[int | None] = mapped_column(Integer, default=None)
    model_descriptor: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class SpecSnapshot(Base):
    """The full agent spec of one publish (`app.services.snapshots`; upstream #277).

    `deployer.pipeline.create_deployment` writes one per create / redeploy / promotion;
    the Runtime-canary promote changes `Agent.spec` without a deployment and writes its
    own, and a startup backfill gives an agent that predates the table a `baseline`.
    Rows are append-only except for the success stamp in `_finish`, which fills
    `aws_version` and replaces `spec` with the spec as deployed (a deploy stage may
    rewrite it mid-job). The evaluation scorecards (`evaluation/scorecard.py`) read
    `seq` as the agent version a run evaluated.

    CN: the rollback route of upstream #277 is not ported (a redeploy publishes any
    spec); the table and its reads are, as the version ledger of upstream #289–#295.
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
    note: Mapped[str | None] = mapped_column(Text, default=None)  # "baseline", …
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
    # Which entrance opened the session: ``console`` (Chat playground) or
    # ``public_api`` (``POST /v1/agents/{id}/invoke[-stream]``). N-SCN-obs1: the
    # /v1 path used to write no row at all, so its sessions were never listed.
    source: Mapped[str] = mapped_column(String(16), default="console")
    # N-ARCH-obs15 v2 (chat parity): the durable claim of the console turn running
    # server-side for this session — its number (= ``turns`` once claimed at ``meta``),
    # when it started and the token that fences release. NULL when nothing runs; a claim
    # found at startup belongs to a dead process and is finalized from the event log.
    active_turn: Mapped[int | None] = mapped_column(default=None)
    active_turn_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    active_turn_token: Mapped[str | None] = mapped_column(String(32), default=None)
    # CN extra (Family B step 1): the console principal that OPENED the session — its
    # `users.id` (NULL for the config admin / auth-disabled operator, for rows older than
    # the column and for a `/v1` session whose key has no creator) and username, display
    # only. A `/v1` session records its key's creator. Members list their own sessions
    # (`routers/chat.py`), administrators every session; the actor stays `actor_id`.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)


class ChatTurnEvent(Base):
    """One SSE event of one console chat turn (N-ARCH-obs15 v2). ``seq`` starts at 1
    per turn and is assigned by the turn's single producer, so ``(agent_id, session_id,
    turn, seq)`` is unique and ordered; heartbeats are never recorded, and the single-use
    as_user authorization ``url`` is dropped from the stored ``auth_required`` frame
    exactly as the history row drops it."""

    __tablename__ = "chat_turn_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agents.id"), index=True)
    session_id: Mapped[str] = mapped_column(String(80), index=True)
    turn: Mapped[int] = mapped_column(default=0)
    seq: Mapped[int] = mapped_column(default=0)
    # meta | tool | tool_input | tool_result | delta | auth_required | policy_denied |
    # policy_decision |
    # truncated | complete | done | error (whatever the wire carried)
    event: Mapped[str] = mapped_column(String(24))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        UniqueConstraint("agent_id", "session_id", "turn", "seq", name="uq_chat_turn_events_seq"),
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
    # N16-obs85 (additive): on a `tool` row, the bounded call summary the runtime streamed —
    # `{id, input (≤ 512 chars), result_summary (≤ 256 chars), status}`; NULL for rows older
    # than the column and for runtimes that stream no summary. Never the full output.
    tool_call: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # N-SCN-bug4: an `agent` row whose answer the runtime closed at the model's token
    # limit (the SSE `truncated: true` marker) — replayed as the bubble's inline notice
    # and as the transcript turn's badge
    # (server default too: pre-workspace rows are inserted by column list — the upgrade
    # tests do — and the migrated column carries DEFAULT 0 as well)
    truncated: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16))  # display only, e.g. lp_live_ab12
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # sha256
    enabled: Mapped[bool] = mapped_column(default=True)
    # The minting console principal (#259): `created_by` is its `users.id`, NULL for the
    # config admin / auth-disabled operator (no `users` row) and for keys minted before
    # creators were recorded by id (the pre-#259 username value moved to
    # `created_by_name` once, see `core.db._migrate_creator_columns`). Not a FK — a
    # deleted creator must leave the id behind, since that dangling id is how the key
    # knows its creator is gone. It is also what the member rule reads: a member lists /
    # toggles / deletes only the keys they minted (N10v2-obs11, re-pointed to the id).
    # `created_by_name` is the display name at mint time (NULL only on legacy rows).
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    # Stamped by /v1 auth on success, at most once per `api_keys.LAST_USED_RESOLUTION`.
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Optional free-text label set at creation (N-SCN-obs4): keys are workspace-wide
    # and outlive agents by design, so the Chat → API panel labels a key with the
    # agent it was minted from — display only, never part of authentication.
    label: Mapped[str | None] = mapped_column(String(120), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


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
    # The creating console principal (`app.services.creators`): `users.id` (NULL for the
    # config admin / auth-disabled operator and for rows whose pre-#261 username — the
    # column used to hold one, `""` when unknown — had no `users` row at migration) + the
    # username at creation, display only.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
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
    # Legacy column from the pre-N3 identity fork, when a Runtime could be
    # JWT-inbound and the vault keyed the user on the inbound JWT instead of
    # the bare user id. Every Runtime inbound is AWS_IAM-only in this build
    # (GRAPH-LOOP-SPEC N3 / D9), so this is always "iam" now; kept as a
    # column (not dropped) so pre-existing rows still load, and in case a
    # future external-IdP node needs the distinction again.
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
