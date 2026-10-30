"""Model registry ledger tables (D21, plan §2.1).

``model_providers`` (one row per model source — hub-global), ``models`` (one row per
catalog model with its capability values verbatim from the N-R6 catalog row),
``model_catalog_changes`` (the immutable journal whose autoincrement id **is** the catalog
version), ``workspace_model_settings`` (per-workspace enable flags; the only
workspace-scoped table — absent row = enabled, only ``false`` rows are written) and, since
r15-e, ``platform_settings`` (the administrator's model-role overrides, hub-global).

Nothing in these tables is a secret: a provider carries the **names** of its SSM parameter
and Identity credential provider, never a value (spec C4; plan §9.2). The registry publishes
an immutable `schemas.model_catalog.Catalog` from these rows; nothing reads them per request.
"""

import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


def new_provider_id() -> str:
    return f"mp_{secrets.token_hex(6)}"


def new_model_id() -> str:
    return f"md_{secrets.token_hex(6)}"


CHANGE_OPERATIONS: tuple[str, ...] = (
    "seed",
    "provider.create", "provider.update", "provider.delete", "provider.credential",
    "provider.probe",
    "model.create", "model.update", "model.delete", "model.probe",
    # N17-obs203 §3: a key typed in the console reached SSM (+ Identity); last4 / name only
    "provider.key",
    "workspace.flags",
    # r15-e (upstream #275 subset): a model-role override set / cleared on the roles tab
    "model_role.set", "model_role.reset",
)


class PlatformSetting(Base):
    """One hub-global row per overridden ``Settings`` field (r15-e, upstream #275 subset) —
    today the model roles of ``app.services.model_roles`` (``key`` = the Settings field,
    ``value`` = the model id, ``""`` where the role allows none). Precedence is console >
    env > launchpad.yaml > default; a missing row means "not overridden here". Hub-global on
    purpose (not in ``WORKSPACE_SCOPED_TABLES``) and per ledger, so each deployment carries
    its own overrides. Column names and types match upstream's table so a ledger written by
    either build reads in the other."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ModelProvider(Base):
    """One model source: Global Bedrock (built-in, required) or an OpenAI-compatible
    endpoint. ``name`` is the former ``PROVIDERS`` key (`bedrock`, `siliconflow`)."""

    __tablename__ = "model_providers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_provider_id)
    name: Mapped[str] = mapped_column(String(32), unique=True)
    kind: Mapped[str] = mapped_column(String(24))  # bedrock | openai_compatible
    label: Mapped[str] = mapped_column(String(64))
    base_url: Mapped[str | None] = mapped_column(String(256), default=None)
    model_region: Mapped[str | None] = mapped_column(String(32), default=None)
    # Identity API-key provider name (derived `launchpad-<name>`, stored for display).
    credential_provider: Mapped[str] = mapped_column(String(128))
    # SSM SecureString parameter NAME the hub reads the key from (`launchpad-<name>-api-key`
    # by default) — never its value.
    credential_ssm_parameter: Mapped[str | None] = mapped_column(String(128), default=None)
    credential_source: Mapped[str] = mapped_column(String(16), default="ssm")  # ssm|host_env
    # Host-env fallback variable name (`LAUNCHPAD_<NAME>_API_KEY`).
    credential_env: Mapped[str | None] = mapped_column(String(64), default=None)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    id_prefix: Mapped[str] = mapped_column(String(48), default="")
    # {state: never|ok|failed, checked_at, latency_ms, models_seen: [..≤200], error}
    probe: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # SSM parameter VERSION + LastModifiedDate of the registered key (never its value) —
    # written by the credential flow (M1-b) so the admin can see a rotation landed.
    key_version: Mapped[int | None] = mapped_column(Integer, default=None)
    key_modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # N17-obs203 §3: a key typed in the console. The console host writes it to the row's SSM
    # SecureString itself (`model_probe.ModelProbeHooks.store_api_key`) and keeps ONLY the last
    # four characters (the "configured · ****abcd" badge) and when it was entered — never the
    # value. A provider whose key the console wrote has its parameter deleted with the row.
    key_last4: Mapped[str | None] = mapped_column(String(4), default=None)
    key_entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # N17-obs203 (Family B): the provider is an OWNER-BOUND resource. `created_by` is the
    # creating account's `users.id` (the ownership key — `services/creators.py`; NULL for the
    # config admin, the seed and every row that predates the column = an administrator's) and
    # `created_by_name` the username at creation (display only). Pre-obs203 ledgers carried a
    # USERNAME in `created_by`; `core/db._migrate_creator_columns` re-points it like the other
    # legacy tables. `scope` is `personal` (visible to its owner and administrators only) or
    # `workspace` (every member sees it; only an administrator may set or flip it).
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    scope: Mapped[str] = mapped_column(String(16), default="workspace")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_by: Mapped[str | None] = mapped_column(String(64), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class Model(Base):
    """One catalog model. The capability columns are the N-R6 catalog row verbatim so the
    snapshot is a dict comprehension over this table."""

    __tablename__ = "models"
    __table_args__ = (
        Index("ix_models_kind_enabled", "kind", "enabled"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_model_id)
    provider_id: Mapped[str] = mapped_column(
        ForeignKey("model_providers.id"), index=True
    )
    model_id: Mapped[str] = mapped_column(String(200), unique=True)  # Launchpad id
    api_model_name: Mapped[str] = mapped_column(String(200))
    label: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), default="chat")  # chat | embedding
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)  # ≤ 1 per kind
    tools: Mapped[bool] = mapped_column(Boolean, default=True)
    streaming: Mapped[bool] = mapped_column(Boolean, default=True)
    vision: Mapped[bool] = mapped_column(Boolean, default=False)
    # {temperature: bool, top_p: bool}
    inference_params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    default_max_tokens: Mapped[int] = mapped_column(Integer, default=4096)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    default_judge_max_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    embedding_dimension: Mapped[int | None] = mapped_column(Integer, default=None)
    embedding_max_chars: Mapped[int | None] = mapped_column(Integer, default=None)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # builtin|probed|manual
    # {field: builtin|probed|manual|unknown}
    capability_sources: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # last probe result (plan §7.4): {checked_at, latency_ms, findings, errors, raw_hint}
    probe: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_by: Mapped[str | None] = mapped_column(String(64), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class ModelCatalogChange(Base):
    """Immutable journal; ``id`` (SQLite AUTOINCREMENT — never reused after a delete) is the
    catalog version stamped on snapshots, `/api/capabilities`, rendered runtimes and job
    payloads. ``before`` / ``after`` are the full provider / model dicts (secret-free by
    construction: nothing secret is stored)."""

    __tablename__ = "model_catalog_changes"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64), default="system")
    target_id: Mapped[str | None] = mapped_column(String(200), default=None)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    # N17-obs203 §5: the visibility scope of the provider the row is about (`personal` |
    # `workspace`; NULL = seed, flags, roles — hub-wide). A `personal` row never becomes the
    # CATALOG VERSION (`model_registry.stamp`): a member's own provider changes nothing a
    # preset snapshot or another member's picker can see, so the `preset_snapshot_behind`
    # banner and `Deployment.catalog_version` stay put. A scope flip is journaled `workspace`
    # (either side shared = the shared catalog changed). The snapshot's `revision`
    # (`content_stamp`) still counts every row, so a second process reloads personal rows too.
    scope: Mapped[str | None] = mapped_column(String(16), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )


class WorkspaceModelSetting(Base):
    """Per-workspace enable flag (plan §2.4). Only ``enabled=False`` rows are written; the
    unique index is restored by `core.db._migrate_model_registry_indexes` after the
    workspace-column rehearsal."""

    __tablename__ = "workspace_model_settings"
    __table_args__ = (
        Index("uq_workspace_model_settings_workspace_model", "workspace_id", "model_id",
              unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True,
                                    default=lambda: secrets.token_hex(16))
    workspace_id: Mapped[str] = mapped_column(String(32), index=True)
    model_id: Mapped[str] = mapped_column(String(200))  # Launchpad id
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[str | None] = mapped_column(String(64), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
