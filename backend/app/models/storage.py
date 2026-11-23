"""The storage catalog (design storage-mounts §4.1): S3 Files / EFS access points an
administrator registers once per workspace, that agents then mount by id.

An agent spec names an entry (`filesystem.mounts[]: {storage_id, mount_path, mode}`)
instead of carrying the access point ARN; the server stamps the resolved kind and ARN
onto the reference when the spec is stored (`services/storage_mounts.normalize_spec_storage`),
and promotion re-resolves it in the target workspace (`services/resource_mapping`, kind
`storage`). A project only sees — and only mounts — the entries in its allowed set
(`project_storage_mounts`); any other one reads as missing (design projects §5.4).

`storage_mounts` and `project_storage_mounts` are workspace-scoped tables
(`core/db.WORKSPACE_SCOPED_TABLES`), so a workspace purge takes them along. Neither
carries `project_id`: an entry is workspace infrastructure shared by the projects
allowed to use it. The catalog id is a slug the administrator picks (`datasets`), unique
per workspace — and the logical name promotion maps by — so the primary key is a
separate surrogate: two workspaces of one hub may both register `datasets`.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _id, _now

KINDS = ("s3files", "efs", "s3bucket")
SCOPES = ("per_session", "shared")  # `s3bucket` only
MODES = ("ro", "rw")
# §8.2: what the data is, which decides how evaluation and the release gate treat it
DATA_CLASSES = ("reference", "shared_state", "scratch")
# `platform` arrives with S3 (the platform creates the file system); v1 registers only
ORIGINS = ("registered", "platform")
# unchecked until the first check; blocked entries cannot be mounted by a new spec
STATUSES = ("unchecked", "ok", "warn", "blocked")
# the shape `schemas.agent.STORAGE_ID_RE` accepts
STORAGE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,31}$"


class StorageMount(Base):
    __tablename__ = "storage_mounts"
    # (workspace_id, id) uniqueness lives in the service: the ledger-migration tests drop
    # `workspace_id` to model an older database, and SQLite cannot drop a column a
    # constraint uses (same as network_profiles)

    pk: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    # the catalog id agents name (`datasets`); also the promotion logical name (§6)
    id: Mapped[str] = mapped_column(String(32), index=True)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))  # KINDS
    # immutable once registered: a reference stamped with it stays right. An `s3bucket`
    # entry has none (SQLite cannot relax NOT NULL on a shipped column): it stores ``""``
    # and every access-point keyed reader skips the kind
    access_point_arn: Mapped[str] = mapped_column(String(2048))
    # filled by the check (GetAccessPoint / DescribeAccessPoints)
    file_system_arn: Mapped[str | None] = mapped_column(String(2048), default=None)
    bucket_arn: Mapped[str | None] = mapped_column(String(2048), default=None)  # s3files only
    posix_uid: Mapped[int | None] = mapped_column(Integer, default=None)
    posix_gid: Mapped[int | None] = mapped_column(Integer, default=None)
    root_directory: Mapped[str | None] = mapped_column(String(1024), default=None)
    # [AvailabilityZoneId] with an available mount target
    mount_target_azs: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    # [{id, subnet_id, availability_zone_id, vpc_id, security_groups, status}] — what the
    # preflight (§5) compares an agent's network profile against
    mount_targets: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    vpc_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # the network profile this storage is usually mounted from (projects §12.3)
    network_profile_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # the highest mode an agent may choose (D-1: read-only unless said otherwise)
    max_mode: Mapped[str] = mapped_column(String(4), default="ro")
    data_class: Mapped[str] = mapped_column(String(16), default="reference")
    origin: Mapped[str] = mapped_column(String(16), default="registered")
    check_results: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    status: Mapped[str] = mapped_column(String(16), default="unchecked")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # `s3bucket` only — what an administrator registers: an existing bucket prefix. All
    # immutable like the access point; ``prefix`` is ``''`` or ``x/y/``
    bucket: Mapped[str | None] = mapped_column(String(63), default=None)
    prefix: Mapped[str | None] = mapped_column(String(1024), default=None)
    scope: Mapped[str | None] = mapped_column(String(16), default=None)  # SCOPES
    bucket_owner: Mapped[str | None] = mapped_column(String(12), default=None)
    bucket_region: Mapped[str | None] = mapped_column(String(32), default=None)
    # filled by the check: ``sse-s3`` | ``sse-kms`` | ``none`` and the key's ARN
    encryption: Mapped[str | None] = mapped_column(String(16), default=None)
    kms_key_arn: Mapped[str | None] = mapped_column(String(2048), default=None)
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class ProjectStorageMount(Base):
    """A project may mount this catalog entry in the entry's workspace (the same shape
    as `project_network_profiles`).

    Keyed on (project, entry): the entry's surrogate key already names its workspace,
    so `workspace_id` stays an ordinary column the workspace purge can match, and
    `storage_id` repeats the slug for reads."""

    __tablename__ = "project_storage_mounts"

    project_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    mount_pk: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    storage_id: Mapped[str] = mapped_column(String(32))
