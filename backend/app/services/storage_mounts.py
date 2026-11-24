"""The storage catalog (design storage-mounts §4–§6): S3 Files / EFS access points and S3
bucket prefixes an administrator registers per workspace, the per-project allowed set that
decides who may mount one, the checks, the agent-time preflight, and what an agent's spec
and IAM role get from a catalog reference.

An ``s3bucket`` entry (AgentCore Runtime V2 ``s3BucketMount``, Mountpoint for S3) is an
existing bucket prefix, not an NFS file system: no access point (the row stores ``""``,
every access-point keyed path skips the kind), no network profile requirement, works on
PUBLIC and VPC, Runtime methods only. Its ``scope`` is ``per_session`` — the platform
mounts ``<entry prefix><agent_id>/{runtimeSessionId}/`` — or ``shared`` (the entry prefix
as it is). Its IAM is prefix-scoped (and ``aws:AgentCoreRuntimeSourceSid``-scoped for
per-session): `storage_iam_statements`, and `boundary_statements` for isolated projects.

Visibility follows design projects §5.4, like network profiles: an entry outside the
current project's allowed set reads as missing (`storage.not_found`), exactly like an id
that does not exist. Administrators see every entry of the workspace.

**Check** (`POST …/{id}/check`, §4.4) reads the access point, its file system and mount
targets through an injectable reader (`set_reader`; the default calls S3 Files'
`GetAccessPoint` / `GetFileSystem` / `ListMountTargets` / `GetMountTarget` or EFS's
`DescribeAccessPoints` / `DescribeFileSystems` / `DescribeMountTargets` /
`DescribeMountTargetSecurityGroups` with the workspace's credentials) and stores the
facts on the entry; with a default network profile it also runs the §5 cross checks.

**Preflight** (§5, `preflight_spec`) runs every catalog mount of a spec against the
spec's network profile: same VPC, a mount target in every subnet's AZ, NFS (TCP 2049)
out of the agent's security groups and into the mount targets'. The subnets and
security groups are read through the workspace's EC2 API (`set_ec2_reader`); a read
failure degrades the network checks to warnings rather than blocking every deploy of a
workspace whose role predates the read grants.

**IAM** (§4.3, `storage_iam_statements`): the mount statements grouped by mode — `rw`
gets `ClientMount` + `ClientWrite`, `ro` only `ClientMount` — keeping the probed shape of
`agent_iam.fs_policy_document` (the `GetAccessPoint` and `ListMountTargets` statements).
"""

import ipaddress
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.models.ledger import Agent
from app.models.storage import ProjectStorageMount, StorageMount
from app.schemas.agent import (
    BUCKET_MOUNT_UNSUPPORTED,
    BUCKET_NAME_RE,
    BUCKET_OWNER_RE,
    BUCKET_REGION_RE,
)
from app.services.agentcore.filesystem_kinds import (
    LIVE_KIND,
    OUTPUTS_DEFAULT_PATH,
    OUTPUTS_SHARED_KEY,
    S3_BUCKET_KIND,
    SCOPE_PER_SESSION,
    SCOPE_SHARED,
    iam_prefix,
    member_kind,
    mounted_prefix,
    outputs_prefix,
)

logger = logging.getLogger(__name__)

NFS_PORT = 2049
S3_BUCKET = "s3bucket"  # the catalog kind (models/storage.KINDS)
# an inline policy of one role is capped at 10,240 characters (whitespace excluded)
MAX_INLINE_POLICY_CHARS = 10240
# the room a per-session prefix leaves for `<agent id>/{runtimeSessionId}/` in S3's 1024
MAX_ENTRY_PREFIX = 900
# the platform's own code / skills bucket (`workspace_bootstrap`): never a mount target
PLATFORM_BUCKET_PREFIX = "launchpad-artifacts-"
_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9!_.'()/-]*$")
_EDITABLE = ("name", "max_mode", "data_class", "network_profile_id")
_MODE_RANK = {"ro": 0, "rw": 1}


def _now() -> datetime:
    return datetime.now(UTC)


def _not_found(storage_id: str) -> NotFoundError:
    return NotFoundError("storage.not_found", f"storage '{storage_id}' not found",
                         {"storage_id": storage_id})


def kind_of_arn(arn: str) -> str | None:
    """`s3files` / `efs` from an access point ARN's service, None for anything else."""
    parts = arn.split(":")
    if len(parts) < 6 or parts[0] != "arn":
        return None
    if parts[2] == "s3files" and "/access-point/" in parts[5]:
        return "s3files"
    if parts[2] == "elasticfilesystem" and parts[5].startswith("access-point/"):
        return "efs"
    return None


def file_system_arn_of(kind: str, access_point_arn: str) -> str | None:
    """An S3 Files AP ARN embeds its file system's; an EFS one does not."""
    if kind == "s3files":
        return access_point_arn.split("/access-point/")[0]
    return None


# ── reads ─────────────────────────────────────────────────────────────────────


def _row(db: Session, workspace_id: str, storage_id: str, *,
         archived: bool = False) -> StorageMount | None:
    query = select(StorageMount).where(StorageMount.workspace_id == workspace_id,
                                       StorageMount.id == storage_id)
    if not archived:
        query = query.where(StorageMount.archived_at.is_(None))
    return db.scalars(query.order_by(StorageMount.created_at.desc())).first()


def allowed_bindings(db: Session, workspace_id: str,
                     project_id: str) -> dict[str, ProjectStorageMount]:
    """The project's allowed set in this workspace: storage id → binding row."""
    rows = db.scalars(select(ProjectStorageMount).where(
        ProjectStorageMount.workspace_id == workspace_id,
        ProjectStorageMount.project_id == project_id))
    return {row.storage_id: row for row in rows}


def _bindings_of(db: Session, row: StorageMount) -> list[ProjectStorageMount]:
    return list(db.scalars(select(ProjectStorageMount).where(
        ProjectStorageMount.mount_pk == row.pk)))


def _projects_by_storage(db: Session, workspace_id: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    rows = db.scalars(select(ProjectStorageMount)
                      .where(ProjectStorageMount.workspace_id == workspace_id)
                      .order_by(ProjectStorageMount.project_id))
    for row in rows:
        out.setdefault(row.storage_id, []).append(row.project_id)
    return out


def spec_mounts(spec: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The catalog references of a stored spec dict (best effort, no validation)."""
    fs = (spec or {}).get("filesystem") or {}
    return [m for m in fs.get("mounts") or [] if isinstance(m, dict) and m.get("storage_id")]


def usage(db: Session, workspace_id: str,
          storage_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Live agents mounting each entry, across every project of the workspace (an entry
    is shared infrastructure; the shared view, §4.5, lists them all)."""
    out: dict[str, list[dict[str, Any]]] = {}
    with project_context(None):
        agents = db.scalars(select(Agent).where(Agent.workspace_id == workspace_id,
                                                Agent.status != "deleted"))
        for agent in agents:
            for mount in spec_mounts(agent.spec):
                sid = mount["storage_id"]
                if storage_id is not None and sid != storage_id:
                    continue
                out.setdefault(sid, []).append({
                    "agent_id": agent.id, "name": agent.name, "project_id": agent.project_id,
                    "version": agent.version, "status": agent.status,
                    "endpoint_mode": agent.endpoint_mode,
                    "mode": mount.get("mode") or "ro",
                    "mount_path": mount.get("mount_path") or f"/mnt/{sid}",
                })
    return out


def to_dict(row: StorageMount, *, allowed: bool = False, projects: list[str] | None = None,
            agents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "name": row.name,
        "kind": row.kind,
        "access_point_arn": row.access_point_arn,
        "file_system_arn": row.file_system_arn,
        "bucket_arn": row.bucket_arn,
        # `s3bucket` entries only (None on the others)
        "bucket": row.bucket,
        "prefix": row.prefix,
        "scope": row.scope,
        "bucket_owner": row.bucket_owner,
        "bucket_region": row.bucket_region,
        "encryption": row.encryption,
        "kms_key_arn": row.kms_key_arn,
        "posix_uid": row.posix_uid,
        "posix_gid": row.posix_gid,
        "root_directory": row.root_directory,
        "mount_target_azs": list(row.mount_target_azs or []),
        "mount_targets": list(row.mount_targets or []),
        "vpc_id": row.vpc_id,
        "network_profile_id": row.network_profile_id,
        "max_mode": row.max_mode,
        "data_class": row.data_class,
        "origin": row.origin,
        "status": row.status,
        "check_results": row.check_results,
        "checked_at": row.checked_at.isoformat() if row.checked_at else None,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
        # relative to the request's project: may its agents mount this entry?
        "allowed": allowed,
    }
    # administrator view only: who else uses it (the shared view, §4.5)
    if projects is not None:
        out["projects"] = projects
    if agents is not None:
        out["agents"] = agents
        writers = sum(1 for a in agents if a["mode"] == "rw")
        out["writers"] = writers
        # S3 Files is close-to-open consistent between NFS clients only: several
        # writers need their own locking or directory split
        out["multi_writer"] = writers > 1
    return out


def list_entries(db: Session, workspace_id: str, project_id: str | None, *, see_all: bool,
                 include_archived: bool = False) -> list[dict[str, Any]]:
    """Members: the current project's allowed set. Administrators: the workspace's
    entries, each with its projects and the agents mounting it."""
    allowed = allowed_bindings(db, workspace_id, project_id) if project_id else {}
    query = select(StorageMount).where(StorageMount.workspace_id == workspace_id)
    if not (see_all and include_archived):
        query = query.where(StorageMount.archived_at.is_(None))
    if not see_all:
        if not allowed:
            return []
        query = query.where(StorageMount.id.in_(sorted(allowed)))
    rows = list(db.scalars(query.order_by(StorageMount.name, StorageMount.id)))
    if not see_all:
        return [to_dict(row, allowed=True) for row in rows]
    projects = _projects_by_storage(db, workspace_id)
    used = usage(db, workspace_id)
    return [to_dict(row, allowed=row.id in allowed, projects=projects.get(row.id, []),
                    agents=used.get(row.id, []) if row.archived_at is None else [])
            for row in rows]


def _visible(db: Session, workspace_id: str, storage_id: str, project_id: str | None, *,
             see_all: bool) -> StorageMount:
    row = _row(db, workspace_id, storage_id)
    if row is None:
        raise _not_found(storage_id)
    if not see_all and (project_id is None
                        or db.get(ProjectStorageMount, (project_id, row.pk)) is None):
        raise _not_found(storage_id)
    return row


def get_entry(db: Session, workspace_id: str, storage_id: str, project_id: str | None, *,
              see_all: bool) -> dict[str, Any]:
    row = _visible(db, workspace_id, storage_id, project_id, see_all=see_all)
    allowed = project_id is not None and db.get(ProjectStorageMount,
                                                (project_id, row.pk)) is not None
    if not see_all:
        return to_dict(row, allowed=allowed)
    out = to_dict(row, allowed=allowed,
                  projects=[b.project_id for b in _bindings_of(db, row)],
                  agents=usage(db, workspace_id, row.id).get(row.id, []))
    if row.kind == S3_BUCKET:
        out["iam_preview"] = bucket_iam_preview(row)
        out["bucket_policy"] = bucket_policy_preview(db, row)
    return out


def entry_usage(db: Session, workspace_id: str, storage_id: str, project_id: str | None, *,
                see_all: bool) -> dict[str, Any]:
    """`GET …/{id}/usage`: the agents mounting the entry. A member sees the ones of
    their own project only (another project's agents stay invisible, §5.4) and the
    workspace-wide writer count, which is what the multi-writer warning is about."""
    row = _visible(db, workspace_id, storage_id, project_id, see_all=see_all)
    agents = usage(db, workspace_id, row.id).get(row.id, [])
    writers = sum(1 for a in agents if a["mode"] == "rw")
    shown = agents if see_all else [a for a in agents if a["project_id"] == project_id]
    return {"storage_id": row.id, "agents": shown, "writers": writers,
            "multi_writer": writers > 1}


# ── writes (administrators) ───────────────────────────────────────────────────


def _assert_profile(db: Session, workspace_id: str, profile_id: str | None) -> None:
    if not profile_id:
        return
    from app.models.network import NetworkProfile

    profile = db.get(NetworkProfile, profile_id)
    if profile is None or profile.workspace_id != workspace_id or profile.archived_at:
        raise NotFoundError("network_profile.not_found",
                            f"network profile '{profile_id}' not found",
                            {"profile_id": profile_id})


def _invalid_bucket(message: str, **detail: Any) -> AppError:
    return AppError("storage.invalid_bucket", message, detail, status_code=422)


def normalize_bucket_prefix(raw: str | None) -> str:
    """An entry prefix in stored form: ``''`` (the whole bucket) or ``x/y/``. It becomes
    the head of an S3 prefix and of IAM ``s3:prefix`` / resource patterns, so it carries
    no wildcard, no variable and no session token: the platform adds the session segment
    of a per-session entry itself."""
    prefix = (raw or "").strip()
    if not prefix:
        return ""
    if "{" in prefix or "}" in prefix or "$" in prefix:
        raise _invalid_bucket("the prefix cannot contain {runtimeSessionId} or any other "
                              "placeholder — the platform adds the session folder itself",
                              prefix=prefix)
    if "*" in prefix or "?" in prefix:
        raise _invalid_bucket("the prefix cannot contain wildcards", prefix=prefix)
    if not _PREFIX_RE.match(prefix) or "//" in prefix:
        raise _invalid_bucket("the prefix must start with a letter or digit and use only "
                              "letters, digits and ! _ . ' ( ) / -", prefix=prefix)
    prefix = prefix if prefix.endswith("/") else f"{prefix}/"
    if len(prefix) > MAX_ENTRY_PREFIX:
        raise _invalid_bucket(f"the prefix is longer than {MAX_ENTRY_PREFIX} characters",
                              prefix=prefix)
    return prefix


def validate_bucket_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """The `StorageMount` columns of a registered S3 bucket entry, validated against the
    service model (bucket 3–63 characters, owner 12 digits, Region shape). Directory
    buckets (``--x-s3``) need ``s3express:CreateSession`` IAM and are refused."""
    bucket = str(fields.get("bucket") or "").strip()
    if not BUCKET_NAME_RE.match(bucket) or ".." in bucket:
        raise _invalid_bucket(f"'{bucket}' is not an S3 bucket name", bucket=bucket)
    if bucket.endswith("--x-s3"):
        raise _invalid_bucket("S3 directory buckets are not supported — register a general "
                              "purpose bucket", bucket=bucket)
    if bucket.startswith(PLATFORM_BUCKET_PREFIX):
        raise _invalid_bucket("the platform's artifact bucket holds agent code and skills "
                              "and cannot be mounted — register a bucket of your own",
                              bucket=bucket)
    scope = fields.get("scope") or SCOPE_PER_SESSION
    if scope not in (SCOPE_PER_SESSION, SCOPE_SHARED):
        raise _invalid_bucket(f"scope must be {SCOPE_PER_SESSION} or {SCOPE_SHARED}",
                              scope=scope)
    owner = str(fields.get("bucket_owner") or "").strip() or None
    if owner and not BUCKET_OWNER_RE.match(owner):
        raise _invalid_bucket("the bucket owner must be a 12-digit account id",
                              bucket_owner=owner)
    region = str(fields.get("bucket_region") or "").strip() or None
    if region and not BUCKET_REGION_RE.match(region):
        raise _invalid_bucket(f"'{region}' is not a Region", bucket_region=region)
    return {"bucket": bucket, "prefix": normalize_bucket_prefix(fields.get("prefix")),
            "scope": scope, "bucket_owner": owner, "bucket_region": region}


def register(db: Session, workspace_id: str, fields: dict[str, Any], actor: str,
             *, origin: str = "registered") -> StorageMount:
    """A new catalog entry (unchecked). The id is unique among the workspace's live
    entries; an archived entry's id may be registered again. ``origin="platform"`` marks
    an entry the console made (`services/storage_provision`), which its teardown may
    delete; a registered one the platform never deletes."""
    storage_id = fields["id"]
    if _row(db, workspace_id, storage_id) is not None:
        raise AppError("storage.id_exists",
                       f"storage '{storage_id}' is already registered in this workspace",
                       {"storage_id": storage_id}, status_code=409)
    _assert_profile(db, workspace_id, fields.get("network_profile_id"))
    common: dict[str, Any] = dict(
        id=storage_id, workspace_id=workspace_id, name=fields.get("name") or storage_id,
        network_profile_id=fields.get("network_profile_id") or None,
        max_mode=fields.get("max_mode") or "ro",
        data_class=fields.get("data_class") or "reference",
        origin=origin, status="unchecked", created_by=actor,
    )
    if fields.get("kind") == S3_BUCKET or fields.get("bucket"):
        row = StorageMount(kind=S3_BUCKET, access_point_arn="",
                           **validate_bucket_fields(fields), **common)
    else:
        arn = (fields.get("access_point_arn") or "").strip()
        kind = kind_of_arn(arn)
        if kind is None or fields.get("kind") not in (None, kind):
            raise AppError("storage.invalid_access_point",
                           f"'{arn}' is not an S3 Files or EFS access point ARN",
                           {"access_point_arn": arn}, status_code=422)
        row = StorageMount(kind=kind, access_point_arn=arn,
                           file_system_arn=file_system_arn_of(kind, arn), **common)
    db.add(row)
    db.commit()
    return row


def update(db: Session, workspace_id: str, storage_id: str, fields: dict[str, Any]) -> dict:
    """Edit the descriptive fields. The access point is immutable (a stamped reference
    must stay right); lowering `max_mode` to `ro` is refused while an agent mounts the
    entry read-write. A new default profile makes the last check stale."""
    row = _row(db, workspace_id, storage_id)
    if row is None:
        raise _not_found(storage_id)
    changes = {k: v for k, v in fields.items() if k in _EDITABLE}
    if changes.get("max_mode") == "ro" and row.max_mode == "rw":
        writers = [a for a in usage(db, workspace_id, row.id).get(row.id, [])
                   if a["mode"] == "rw"]
        if writers:
            raise AppError("storage.mode_exceeds_max",
                           f"{len(writers)} agent(s) mount '{row.name}' read-write — switch "
                           "them to read-only first",
                           {"storage_id": row.id, "agents": [a["agent_id"] for a in writers]},
                           status_code=409)
    if "network_profile_id" in changes:
        changes["network_profile_id"] = changes["network_profile_id"] or None
        _assert_profile(db, workspace_id, changes["network_profile_id"])
        if changes["network_profile_id"] != row.network_profile_id:
            row.status, row.check_results, row.checked_at = "unchecked", None, None
    for key, value in changes.items():
        if value is not None or key == "network_profile_id":
            setattr(row, key, value)
    row.updated_at = _now()
    db.commit()
    return get_entry(db, workspace_id, storage_id, None, see_all=True)


def assert_unused(db: Session, workspace_id: str, row: StorageMount) -> None:
    """`storage.in_use` (409) while a live agent mounts the entry."""
    used = usage(db, workspace_id, row.id).get(row.id, [])
    if used:
        raise AppError("storage.in_use",
                       f"{len(used)} agent(s) still mount '{row.name}' — remove the mount "
                       "from them first",
                       {"storage_id": row.id, "agents": [a["agent_id"] for a in used]},
                       status_code=409)


def archive(db: Session, workspace_id: str, storage_id: str) -> None:
    """Archive (the row stays: snapshots may still name it). The platform never deletes
    registered storage itself (design principle 4). Refused while a live agent mounts it."""
    row = _row(db, workspace_id, storage_id)
    if row is None:
        raise _not_found(storage_id)
    assert_unused(db, workspace_id, row)
    row.archived_at = _now()
    row.updated_at = row.archived_at
    for binding in _bindings_of(db, row):
        db.delete(binding)
    db.commit()


# ── the per-project allowed set ───────────────────────────────────────────────


def project_entries(db: Session, workspace_id: str, project_id: str) -> dict[str, Any]:
    from app.services.network_profiles import assert_project_in_workspace

    assert_project_in_workspace(db, workspace_id, project_id)
    allowed = allowed_bindings(db, workspace_id, project_id)
    rows = [db.get(StorageMount, b.mount_pk) for b in allowed.values()]
    live = sorted((r for r in rows if r is not None and r.archived_at is None),
                  key=lambda r: (r.name, r.id))
    return {"project_id": project_id, "workspace_id": workspace_id,
            "storage": [to_dict(r, allowed=True) for r in live]}


def set_project_entries(db: Session, workspace_id: str, project_id: str,
                        storage_ids: list[str]) -> dict[str, Any]:
    """Replace the project's allowed set in this workspace. Taking an entry away from a
    project whose agents mount it is allowed (like network profiles): their next
    re-publish is refused until the mount is removed or the entry allowed again."""
    from app.services.network_profiles import assert_project_in_workspace

    assert_project_in_workspace(db, workspace_id, project_id)
    wanted: dict[str, StorageMount] = {}
    for sid in dict.fromkeys(storage_ids):
        row = _row(db, workspace_id, sid)
        if row is None:
            raise _not_found(sid)
        wanted[sid] = row
    current = allowed_bindings(db, workspace_id, project_id)
    for sid, binding in current.items():
        if sid not in wanted or wanted[sid].pk != binding.mount_pk:
            db.delete(binding)
    db.flush()
    for sid, row in wanted.items():
        if db.get(ProjectStorageMount, (project_id, row.pk)) is None:
            db.add(ProjectStorageMount(project_id=project_id, mount_pk=row.pk,
                                       workspace_id=workspace_id, storage_id=sid))
    db.commit()
    return project_entries(db, workspace_id, project_id)


# ── reading the storage (§4.4) ────────────────────────────────────────────────

# (ctx, kind, access point ARN) → facts:
# {access_point: {status, file_system_id, posix_uid, posix_gid, root_directory} | None,
#  file_system: {arn, status, bucket_arn} | None,
#  mount_targets: [{id, subnet_id, availability_zone_id, vpc_id, ip, security_groups,
#                   status}]}
StorageReader = Callable[[Any, str, str], dict[str, Any]]


def _not_found_code(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code", "")
    return code in ("ResourceNotFoundException", "AccessPointNotFound", "NotFoundException",
                    "FileSystemNotFound")


def _read_s3files(ctx: Any, arn: str) -> dict[str, Any]:
    client = ctx.client("s3files")
    try:
        ap = client.get_access_point(accessPointId=arn)
    except ClientError as exc:
        if _not_found_code(exc):
            return {"access_point": None, "file_system": None, "mount_targets": []}
        raise
    posix = ap.get("posixUser") or {}
    fs_id = ap.get("fileSystemId") or ""
    facts: dict[str, Any] = {
        "access_point": {"status": ap.get("status"), "file_system_id": fs_id,
                         "posix_uid": posix.get("uid"), "posix_gid": posix.get("gid"),
                         "root_directory": (ap.get("rootDirectory") or {}).get("path")},
        "file_system": None, "mount_targets": [],
    }
    fs = client.get_file_system(fileSystemId=fs_id)
    facts["file_system"] = {"arn": fs.get("fileSystemArn"), "status": fs.get("status"),
                            "bucket_arn": fs.get("bucket")}
    kwargs: dict[str, Any] = {"fileSystemId": fs_id}
    while True:
        page = client.list_mount_targets(**kwargs)
        for mt in page.get("mountTargets") or []:
            detail = client.get_mount_target(mountTargetId=mt["mountTargetId"])
            facts["mount_targets"].append({
                "id": mt.get("mountTargetId"), "subnet_id": mt.get("subnetId"),
                "availability_zone_id": mt.get("availabilityZoneId"),
                "vpc_id": mt.get("vpcId"), "ip": mt.get("ipv4Address"),
                "security_groups": list(detail.get("securityGroups") or []),
                "status": mt.get("status"),
            })
        token = page.get("nextToken")
        if not token:
            return facts
        kwargs["nextToken"] = token


def _read_efs(ctx: Any, arn: str) -> dict[str, Any]:
    client = ctx.client("efs")
    ap_id = arn.rsplit("/", 1)[-1]
    try:
        aps = client.describe_access_points(AccessPointId=ap_id).get("AccessPoints") or []
    except ClientError as exc:
        if _not_found_code(exc):
            return {"access_point": None, "file_system": None, "mount_targets": []}
        raise
    if not aps:
        return {"access_point": None, "file_system": None, "mount_targets": []}
    ap = aps[0]
    posix = ap.get("PosixUser") or {}
    fs_id = ap.get("FileSystemId") or ""
    facts: dict[str, Any] = {
        "access_point": {"status": ap.get("LifeCycleState"), "file_system_id": fs_id,
                         "posix_uid": posix.get("Uid"), "posix_gid": posix.get("Gid"),
                         "root_directory": (ap.get("RootDirectory") or {}).get("Path")},
        "file_system": None, "mount_targets": [],
    }
    systems = client.describe_file_systems(FileSystemId=fs_id).get("FileSystems") or []
    if systems:
        facts["file_system"] = {"arn": systems[0].get("FileSystemArn"),
                                "status": systems[0].get("LifeCycleState"), "bucket_arn": None}
    kwargs: dict[str, Any] = {"FileSystemId": fs_id}
    while True:
        page = client.describe_mount_targets(**kwargs)
        for mt in page.get("MountTargets") or []:
            groups = client.describe_mount_target_security_groups(
                MountTargetId=mt["MountTargetId"]).get("SecurityGroups") or []
            facts["mount_targets"].append({
                "id": mt.get("MountTargetId"), "subnet_id": mt.get("SubnetId"),
                "availability_zone_id": mt.get("AvailabilityZoneId"),
                "vpc_id": mt.get("VpcId"), "ip": mt.get("IpAddress"),
                "security_groups": list(groups), "status": mt.get("LifeCycleState"),
            })
        marker = page.get("NextMarker")
        if not marker:
            return facts
        kwargs["Marker"] = marker


def _read_storage(ctx: Any, kind: str, arn: str) -> dict[str, Any]:
    return _read_s3files(ctx, arn) if kind == "s3files" else _read_efs(ctx, arn)


_reader: StorageReader = _read_storage


def set_reader(reader: StorageReader) -> StorageReader:
    """Swap the storage reader (tests); returns the previous one."""
    global _reader
    previous, _reader = _reader, reader
    return previous


# (ctx, subnet ids, security group ids) → {subnets: [ec2 Subnet], groups: [ec2 SG]}
Ec2Reader = Callable[[Any, list[str], list[str]], dict[str, list[dict[str, Any]]]]


def _read_ec2(ctx: Any, subnet_ids: list[str], group_ids: list[str]) -> dict[str, list]:
    from app.services.network_preflight import _pages

    ec2 = ctx.client("ec2")
    subnets = _pages(ec2.describe_subnets, "Subnets", Filters=[
        {"Name": "subnet-id", "Values": list(subnet_ids)}]) if subnet_ids else []
    groups = _pages(ec2.describe_security_groups, "SecurityGroups", Filters=[
        {"Name": "group-id", "Values": list(group_ids)}]) if group_ids else []
    return {"subnets": subnets, "groups": groups}


_ec2_reader: Ec2Reader = _read_ec2


def set_ec2_reader(reader: Ec2Reader) -> Ec2Reader:
    """Swap the EC2 reader (tests); returns the previous one."""
    global _ec2_reader
    previous, _ec2_reader = _ec2_reader, reader
    return previous


def _aws_code(exc: Exception) -> str:
    return (exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError)
            else type(exc).__name__) or "Unknown"


def _check_failed(exc: Exception, kind: str) -> AppError:
    code = _aws_code(exc)
    hint = (" — update the workspace role from the current infra/spoke template (it grants "
            "the read-only S3 Files / EFS describe calls the check makes)"
            if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation")
            else "")
    label = "S3 Files" if kind == "s3files" else "EFS"
    return AppError("storage.check_failed",
                    f"could not read this {label} access point ({code}){hint}",
                    {"aws_code": code}, status_code=502)


def _bucket_check_failed(exc: Exception) -> AppError:
    code = _aws_code(exc)
    hint = (" — update the workspace role from the current infra/spoke template (v3 grants "
            "the read-only S3 bucket calls the check makes)"
            if code in ("AccessDenied", "AccessDeniedException", "403") else "")
    return AppError("storage.check_failed",
                    f"could not read this S3 bucket ({code}){hint}",
                    {"aws_code": code}, status_code=502)


def _result(check: str, severity: str, message: str, **details: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"check": check, "severity": severity, "message": message}
    if details:
        out["details"] = details
    return out


def _available(status: Any) -> bool:
    return str(status or "").lower() == "available"


def storage_results(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """The storage-only checks of an entry: the access point, its file system, mount
    targets, and whether they sit in one VPC."""
    out: list[dict[str, Any]] = []
    ap = facts.get("access_point")
    if ap is None:
        out.append(_result("access_point", "block",
                           "the access point was not found in this account and region"))
        return out
    out.append(_result("access_point", "ok" if _available(ap.get("status")) else "block",
                       "the access point is available" if _available(ap.get("status"))
                       else f"the access point is {ap.get('status')}"))
    fs = facts.get("file_system")
    fs_ok = fs is not None and _available(fs.get("status"))
    out.append(_result("file_system", "ok" if fs_ok else "block",
                       "the file system is available" if fs_ok else
                       f"the file system is {fs.get('status') if fs else 'missing'}"))
    targets = [t for t in facts.get("mount_targets") or [] if _available(t.get("status"))]
    azs = sorted({t["availability_zone_id"] for t in targets if t.get("availability_zone_id")})
    out.append(_result("mount_targets", "ok" if targets else "block",
                       f"{len(targets)} available mount target(s) in {', '.join(azs)}"
                       if targets else "the file system has no available mount target — "
                       "agents could not reach it", azs=azs))
    vpcs = sorted({t["vpc_id"] for t in targets if t.get("vpc_id")})
    if len(vpcs) > 1:
        out.append(_result("mount_targets.vpc", "warn",
                           f"mount targets sit in several VPCs ({', '.join(vpcs)})", vpcs=vpcs))
    return out


# ── S3 bucket entries: the read, the checks ───────────────────────────────────

# (ctx, bucket, bucket owner | None) → facts:
# {exists, reachable, region, encryption, kms_key_arn, public_access_blocked, versioning,
#  unreadable: [what a missing permission kept from being read]}
BucketReader = Callable[[Any, str, str | None], dict[str, Any]]

_BUCKET_ACCESS_CODES = frozenset({"403", "AccessDenied", "AccessDeniedException",
                                  "AllAccessDisabled"})
_BUCKET_MISSING_CODES = frozenset({"404", "NoSuchBucket", "NotFound"})


def _error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _kms_key_arn(ctx: Any, key: str, bucket_region: str | None, owner: str | None) -> str | None:
    """The ARN of the key a bucket default names (an ARN, a key id, or an alias; an alias
    is resolved through the workspace's own KMS endpoint, so a key elsewhere stays
    unresolved and the check says so)."""
    if key.startswith("arn:") and ":alias/" not in key:
        return key
    if key.startswith("arn:") or key.startswith("alias/"):
        try:
            described = ctx.client("kms").describe_key(KeyId=key)
        except (ClientError, BotoCoreError):
            return None
        return (described.get("KeyMetadata") or {}).get("Arn")
    region = bucket_region or ctx.region
    return f"arn:{ctx.partition}:kms:{region}:{owner or ctx.account_id}:key/{key}"


def _read_s3bucket(ctx: Any, bucket: str, owner: str | None) -> dict[str, Any]:
    """The check's reads of a bucket (workspace credentials, no writes). Only HeadBucket is
    essential; a missing permission on the others is reported, not raised."""
    client = ctx.client("s3")
    facts: dict[str, Any] = {"exists": True, "reachable": True, "region": None,
                             "encryption": None, "kms_key_arn": None,
                             "public_access_blocked": None, "versioning": None,
                             "unreadable": []}
    head_args: dict[str, Any] = {"Bucket": bucket}
    if owner:
        head_args["ExpectedBucketOwner"] = owner
    try:
        head = client.head_bucket(**head_args)
    except ClientError as exc:
        code = _error_code(exc)
        if code in _BUCKET_MISSING_CODES:
            return {**facts, "exists": False, "reachable": False}
        if code in _BUCKET_ACCESS_CODES:
            return {**facts, "reachable": False}
        raise
    facts["region"] = ((head.get("ResponseMetadata") or {}).get("HTTPHeaders") or {}).get(
        "x-amz-bucket-region")
    if facts["region"] is None:
        try:
            location = client.get_bucket_location(Bucket=bucket).get("LocationConstraint")
            facts["region"] = {None: "us-east-1", "": "us-east-1",
                               "EU": "eu-west-1"}.get(location, location)
        except ClientError:
            facts["unreadable"].append("region")
    try:
        rules = (client.get_bucket_encryption(Bucket=bucket).get(
            "ServerSideEncryptionConfiguration") or {}).get("Rules") or []
        default = (rules[0].get("ApplyServerSideEncryptionByDefault") or {}) if rules else {}
        algorithm = str(default.get("SSEAlgorithm") or "")
        if algorithm.startswith("aws:kms"):
            facts["encryption"] = "sse-kms"
            key = str(default.get("KMSMasterKeyID") or "")
            facts["kms_key_arn"] = _kms_key_arn(ctx, key, facts["region"], owner) if key else None
        else:
            facts["encryption"] = "sse-s3" if algorithm else "none"
    except ClientError as exc:
        if _error_code(exc) == "ServerSideEncryptionConfigurationNotFoundError":
            facts["encryption"] = "none"
        else:
            facts["unreadable"].append("encryption")
    try:
        block = client.get_public_access_block(Bucket=bucket).get(
            "PublicAccessBlockConfiguration") or {}
        facts["public_access_blocked"] = all(block.get(k) for k in (
            "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"))
    except ClientError as exc:
        if _error_code(exc) == "NoSuchPublicAccessBlockConfiguration":
            facts["public_access_blocked"] = False
        else:
            facts["unreadable"].append("public_access_block")
    try:
        facts["versioning"] = client.get_bucket_versioning(Bucket=bucket).get("Status") or "Off"
    except ClientError:
        facts["unreadable"].append("versioning")
    return facts


_bucket_reader: BucketReader = _read_s3bucket


def set_bucket_reader(reader: BucketReader) -> BucketReader:
    """Swap the S3 bucket reader (tests); returns the previous one."""
    global _bucket_reader
    previous, _bucket_reader = _bucket_reader, reader
    return previous


def bucket_results(facts: dict[str, Any], row: Any, workspace_region: str) -> list[dict[str, Any]]:
    """The checks of a bucket entry: reachable, in the Region the mount will ask for,
    encryption (and the KMS grant it implies), Block Public Access, versioning."""
    out: list[dict[str, Any]] = []
    bucket = row.bucket
    if not facts.get("exists", True):
        return [_result("s3.bucket_missing", "block",
                        f"the bucket '{bucket}' was not found — check the name",
                        bucket=bucket)]
    if not facts.get("reachable", True):
        owner = f" (expected owner {row.bucket_owner})" if row.bucket_owner else ""
        return [_result("s3.bucket_access", "block",
                        f"access to '{bucket}' is denied{owner} — the bucket owner is wrong, "
                        "or the workspace role lacks read access to the bucket (spoke "
                        "template v3 grants it; a cross-account bucket also needs the "
                        "owner's bucket policy)", bucket=bucket,
                        bucket_owner=row.bucket_owner)]
    out.append(_result("s3.bucket", "ok", f"the bucket '{bucket}' is reachable"))
    expected = row.bucket_region or workspace_region
    actual = facts.get("region")
    if actual and actual != expected:
        out.append(_result(
            "s3.region_mismatch", "block",
            f"the bucket is in {actual}, the mount will look in {expected} — AgentCore "
            f"rejects the mount; register it again with the bucket Region set to {actual}",
            bucket_region=actual, expected=expected))
    elif actual:
        out.append(_result("s3.region", "ok", f"the bucket is in {actual}"))
    encryption = facts.get("encryption")
    if encryption == "sse-kms":
        if facts.get("kms_key_arn"):
            out.append(_result("s3.kms", "info",
                               "the bucket encrypts with a KMS key — agents get "
                               "kms:Decrypt" + (" and kms:GenerateDataKey" if
                                                row.max_mode == "rw" else "")
                               + " on it (a key in another account needs its own key "
                               "policy)", kms_key_arn=facts["kms_key_arn"]))
        else:
            out.append(_result("s3.kms", "warn",
                               "the bucket encrypts with a KMS key the check could not "
                               "resolve to an ARN — mounts will fail until the role may use it"))
    elif encryption in ("sse-s3", "none"):
        out.append(_result("s3.encryption", "ok" if encryption == "sse-s3" else "info",
                           "the bucket encrypts with SSE-S3" if encryption == "sse-s3"
                           else "the bucket has no default encryption"))
    if facts.get("public_access_blocked") is False:
        out.append(_result("s3.public_access", "warn",
                           "Block Public Access is not fully on for this bucket — turn it on "
                           "unless the bucket is meant to be public"))
    elif facts.get("public_access_blocked"):
        out.append(_result("s3.public_access", "ok", "Block Public Access is on"))
    if facts.get("versioning"):
        out.append(_result("s3.versioning", "info",
                           f"versioning is {str(facts['versioning']).lower()}"))
    for what in facts.get("unreadable") or []:
        out.append(_result(f"s3.unreadable.{what}", "warn",
                           f"could not read the bucket's {what.replace('_', ' ')} — update "
                           "the workspace role from the current infra/spoke template"))
    return out


def _bucket_vpc_results(ctx: Any, network: dict[str, Any], entry: dict[str, Any],
                        ) -> list[dict[str, Any]]:
    """A VPC agent reaches S3 over a gateway endpoint or a NAT gateway. Warnings only:
    the route tables are read, not proven by a real mount (the live probe P8 stays
    doc-based)."""
    from app.services import network_preflight, storage_provision

    sid = entry["id"]

    def result(check: str, severity: str, message: str, **details: Any) -> dict[str, Any]:
        return {**_result(check, severity, message, **details), "storage_id": sid}

    out: list[dict[str, Any]] = []
    region = entry.get("bucket_region") or ctx.region
    if region != ctx.region:
        out.append(result("storage.s3_region", "warn",
                          f"{sid} is in {region}, the agent's network is in {ctx.region} — a "
                          "gateway endpoint serves only its own Region, so the network needs "
                          "a NAT gateway or another route to that Region"))
    try:
        facts = network_preflight.gather(
            ctx.client("ec2"), vpc_id=network.get("vpc_id"),
            subnet_ids=list(network.get("subnet_ids") or []), security_group_ids=[])
    except (ClientError, BotoCoreError) as exc:
        return [*out, result("storage.s3_endpoint", "warn",
                             f"the agent's network could not be read ({_aws_code(exc)}) — a "
                             "route to S3 is not verified")]
    gateway = storage_provision.s3_gateway_endpoint(facts, ctx.region)
    if gateway["status"] == "ok":
        out.append(result("storage.s3_endpoint", "ok",
                          "an S3 gateway endpoint serves the agent's subnets"))
    elif facts.get("nat_gateways"):
        out.append(result("storage.s3_endpoint", "info",
                          "no S3 gateway endpoint serves the agent's subnets; the mount "
                          "reaches S3 through the NAT gateway"))
    else:
        out.append(result("storage.s3_endpoint", "warn",
                          "no S3 gateway endpoint or NAT gateway serves the agent's "
                          f"subnets ({gateway['status']}) — the mount could not reach S3",
                          unrouted_subnets=gateway.get("unrouted_subnets")))
    return out


def evaluate_bucket_mount(entry: dict[str, Any], *, mode: str = "ro", method: str = "",
                          platform_version: str | None = None) -> list[dict[str, Any]]:
    """The agent-time checks of one S3 bucket mount that need no AWS read: the entry's
    state and mode, the method (the Harness drops the member) and the platform version
    (V1 refuses it at create)."""
    sid = entry["id"]
    out = _entry_results(entry, mode)
    if method in BUCKET_MOUNT_UNSUPPORTED:
        out.append({**_result("storage.method", "block", BUCKET_MOUNT_UNSUPPORTED[method]),
                    "storage_id": sid})
    if platform_version == "V1":
        out.append({**_result("storage.requires_v2", "block",
                              "S3 bucket mounts need Runtime platform version V2 — switch "
                              "the agent to V2 in its Runtime settings"), "storage_id": sid})
    return out


# ── storage × network × agent (§5) ────────────────────────────────────────────

# the uid a method's runtime runs as, once probe P6 (§3.1) has said; empty until then,
# so the POSIX check stays informational
KNOWN_RUN_UIDS: dict[str, int] = {}


def _covers_nfs(rule: dict[str, Any]) -> bool:
    proto = str(rule.get("IpProtocol", ""))
    if proto == "-1":
        return True
    if proto not in ("tcp", "6"):
        return False
    low, high = rule.get("FromPort"), rule.get("ToPort")
    return low is None or high is None or (low <= NFS_PORT <= high)


def _cidr_holds(cidr: str, inner: str) -> bool:
    try:
        return ipaddress.ip_network(inner, strict=False).subnet_of(
            ipaddress.ip_network(cidr, strict=False))
    except (TypeError, ValueError):
        return False


def _nfs_out(groups: list[dict[str, Any]], target_groups: set[str],
             target_ips: list[str]) -> bool:
    """Some agent SG lets TCP 2049 out to a mount target SG, or to a CIDR holding every
    mount target address (0.0.0.0/0 included)."""
    for group in groups:
        for rule in group.get("IpPermissionsEgress") or []:
            if not _covers_nfs(rule):
                continue
            if any(p.get("GroupId") in target_groups for p in rule.get("UserIdGroupPairs") or []):
                return True
            for item in rule.get("IpRanges") or []:
                cidr = item.get("CidrIp", "")
                if cidr == "0.0.0.0/0" or (target_ips and all(
                        _cidr_holds(cidr, f"{ip}/32") for ip in target_ips)):
                    return True
    return False


def _nfs_in(target_groups: list[dict[str, Any]], agent_groups: set[str],
            subnet_cidrs: list[str]) -> bool:
    """Some mount target SG admits TCP 2049 from an agent SG, or from a CIDR holding
    every agent subnet."""
    for group in target_groups:
        for rule in group.get("IpPermissions") or []:
            if not _covers_nfs(rule):
                continue
            if any(p.get("GroupId") in agent_groups for p in rule.get("UserIdGroupPairs") or []):
                return True
            for item in rule.get("IpRanges") or []:
                cidr = item.get("CidrIp", "")
                if cidr == "0.0.0.0/0" or (subnet_cidrs and all(
                        _cidr_holds(cidr, s) for s in subnet_cidrs)):
                    return True
    return False


def _entry_results(entry: dict[str, Any], mode: str) -> list[dict[str, Any]]:
    """The state of the catalog entry itself and the mode asked of it (every kind)."""
    sid = entry["id"]
    out: list[dict[str, Any]] = []

    def add(check: str, severity: str, message: str) -> None:
        out.append({**_result(check, severity, message), "storage_id": sid})

    if entry.get("archived_at"):
        add("storage.status", "block", f"storage {sid} is archived")
    elif entry.get("status") == "blocked":
        add("storage.status", "block", f"storage {sid} failed its check — fix it and re-run "
            "the check")
    elif entry.get("status") == "unchecked":
        add("storage.status", "warn", f"storage {sid} has not been checked yet")
    else:
        add("storage.status", "ok", f"storage {sid} is {entry.get('status')}")
    if _MODE_RANK.get(mode, 0) > _MODE_RANK.get(entry.get("max_mode") or "ro", 0):
        add("storage.mode", "block", f"storage {sid} may be mounted {entry.get('max_mode')} "
            "at most")
    return out


def evaluate_mount(
    entry: dict[str, Any], network: dict[str, Any], *, mode: str = "ro", method: str = "",
    byoc_image: bool = False, gated: bool = False,
) -> list[dict[str, Any]]:
    """The §5 checks of one mount of `entry` (a `to_dict` view) from an agent placed in
    `network`: ``{vpc_id, subnet_ids, security_group_ids, subnets (EC2 rows | None),
    groups (EC2 rows by id | None), azs (AZ ids | None), dns (bool | None),
    read_error}``. Every result carries ``storage_id``."""
    sid = entry["id"]
    out: list[dict[str, Any]] = []

    def add(check: str, severity: str, message: str, **details: Any) -> None:
        out.append({**_result(check, severity, message, **details), "storage_id": sid})

    # 1 — the entry itself
    out.extend(_entry_results(entry, mode))
    targets = [t for t in entry.get("mount_targets") or [] if _available(t.get("status"))]
    if not targets:
        add("storage.mount_targets", "warn" if entry.get("status") == "unchecked" else "block",
            f"no known mount target of {sid} — run its check" if entry.get("status") ==
            "unchecked" else f"{sid} has no available mount target")
        return out
    # 2 — one VPC
    mt_vpcs = sorted({t["vpc_id"] for t in targets if t.get("vpc_id")})
    vpc = network.get("vpc_id")
    if vpc and mt_vpcs and vpc not in mt_vpcs:
        add("storage.vpc", "block", f"{sid}'s mount targets are in {', '.join(mt_vpcs)}, the "
            f"agent's network is in {vpc}", mount_target_vpcs=mt_vpcs, vpc_id=vpc)
    elif vpc and mt_vpcs:
        add("storage.vpc", "ok", f"{sid} and the agent's network share {vpc}")
    # 3 — a mount target in every subnet's AZ
    mt_azs = {t["availability_zone_id"] for t in targets if t.get("availability_zone_id")}
    subnets = network.get("subnets")
    if subnets is not None:
        missing = {s["SubnetId"]: s.get("AvailabilityZoneId") for s in subnets
                   if s.get("AvailabilityZoneId") not in mt_azs}
        add("storage.azs", "block" if missing else "ok",
            "; ".join(f"{s} ({az}) is in an Availability Zone with no mount target of {sid} "
                      "— a session placed there times out mounting it"
                      for s, az in missing.items()) if missing
            else f"every subnet's Availability Zone has a mount target of {sid}",
            subnets=sorted(missing))
    elif network.get("azs") is not None:
        missing_az = sorted(set(network["azs"]) - mt_azs)
        add("storage.azs", "block" if missing_az else "ok",
            f"Availability Zone(s) {', '.join(missing_az)} of the agent's subnets have no "
            f"mount target of {sid}" if missing_az
            else f"every subnet's Availability Zone has a mount target of {sid}",
            azs=missing_az)
    else:
        add("storage.azs", "warn", "the agent's subnets could not be read — Availability "
            "Zone coverage is not verified")
    # 4, 5 — NFS between the agent and the mount targets
    groups = network.get("groups")
    mt_group_ids = sorted({g for t in targets for g in t.get("security_groups") or []})
    if groups is None:
        add("storage.nfs", "warn", "the security groups could not be read"
            + (f" ({network['read_error']})" if network.get("read_error") else "")
            + " — TCP 2049 between the agent and the mount targets is not verified")
    else:
        agent_groups = [groups[g] for g in network.get("security_group_ids") or [] if g in groups]
        ips = [t["ip"] for t in targets if t.get("ip")]
        out_ok = _nfs_out(agent_groups, set(mt_group_ids), ips)
        add("storage.egress_2049", "ok" if out_ok else "block",
            "the agent's security groups allow TCP 2049 out to the mount targets" if out_ok
            else f"{', '.join(network.get('security_group_ids') or [])} allow no outbound TCP "
            f"2049 to {', '.join(mt_group_ids) or 'the mount targets'}")
        mt_groups = [groups[g] for g in mt_group_ids if g in groups]
        if not mt_group_ids or len(mt_groups) < len(mt_group_ids):
            add("storage.ingress_2049", "warn", "the mount targets' security groups could not "
                "be read — inbound TCP 2049 is not verified")
        else:
            cidrs = [s.get("CidrBlock") for s in subnets or [] if s.get("CidrBlock")]
            in_ok = _nfs_in(mt_groups, set(network.get("security_group_ids") or []), cidrs)
            add("storage.ingress_2049", "ok" if in_ok else "block",
                "the mount targets admit TCP 2049 from the agent" if in_ok
                else f"{', '.join(mt_group_ids)} admit no inbound TCP 2049 from "
                f"{', '.join(network.get('security_group_ids') or [])}")
    # 6 — DNS (the file system's host name); the profile preflight already reads it
    if network.get("dns") is False:
        add("storage.dns", "block", "the VPC needs DNS resolution and DNS hostnames on to "
            "resolve the file system's name")
    # 7 — POSIX identity
    uid = entry.get("posix_uid")
    if uid is not None:
        run_uid = KNOWN_RUN_UIDS.get(method)
        if run_uid is not None and run_uid != uid:
            add("storage.posix", "warn", f"the access point acts as uid {uid}, the {method} "
                f"runtime runs as {run_uid} — writes may fail", posix_uid=uid, run_uid=run_uid)
        else:
            add("storage.posix", "info", f"the access point acts as uid {uid}:"
                f"{entry.get('posix_gid')} — make sure the agent's files allow it")
    # 8 — architecture of a brought image
    if byoc_image:
        add("storage.arch", "info", "AgentCore runs linux/arm64 — a brought image must be "
            "built for it")
    # 9 — shared state written by a gated workspace's release gate (§8.2)
    if mode == "rw" and entry.get("data_class") == "shared_state" and gated:
        add("storage.shared_state", "warn", f"{sid} is shared state mounted read-write: the "
            "release gate's evaluation writes into it")
    return out


def _network_facts(db: Session, ctx: Any, network: Any) -> dict[str, Any] | None:
    """What `evaluate_mount` reads about an agent's network (None: PUBLIC)."""
    from app.models.network import NetworkProfile
    from app.services.network_profiles import network_profile_id

    if not network:
        return None
    pid = network_profile_id(network)
    facts: dict[str, Any] = {"vpc_id": None, "subnets": None, "groups": None, "azs": None,
                             "dns": None, "read_error": None}
    if pid is not None:
        row = db.get(NetworkProfile, pid)
        if row is None or row.workspace_id != ctx.id:
            return {**facts, "subnet_ids": [], "security_group_ids": [], "missing": pid}
        subnet_ids, group_ids = list(row.subnet_ids or []), list(row.security_group_ids or [])
        facts["vpc_id"] = row.vpc_id
        for r in row.check_results or []:
            if r.get("check") == "subnets.azs":
                facts["azs"] = list((r.get("details") or {}).get("azs") or []) or None
            elif r.get("check") == "vpc.dns":
                facts["dns"] = r.get("severity") == "ok"
    else:
        data = network if isinstance(network, dict) else network.model_dump()
        subnet_ids = list(data.get("subnets") or [])
        group_ids = list(data.get("security_groups") or [])
    facts["subnet_ids"], facts["security_group_ids"] = subnet_ids, group_ids
    facts["profile_id"] = pid
    return facts


def _read_network(ctx: Any, facts: dict[str, Any], extra_groups: list[str]) -> None:
    try:
        read = _ec2_reader(ctx, facts["subnet_ids"],
                           sorted({*facts["security_group_ids"], *extra_groups}))
    except (ClientError, BotoCoreError) as exc:
        facts["read_error"] = _aws_code(exc)
        return
    facts["subnets"] = read.get("subnets") or []
    facts["groups"] = {g["GroupId"]: g for g in read.get("groups") or []}
    if facts["vpc_id"] is None:
        vpcs = sorted({s["VpcId"] for s in facts["subnets"] if s.get("VpcId")})
        facts["vpc_id"] = vpcs[0] if len(vpcs) == 1 else None


def status_of(results: list[dict[str, Any]]) -> str:
    severities = {r.get("severity") for r in results}
    if "block" in severities:
        return "blocked"
    if "warn" in severities:
        return "warn"
    return "ok"


def run_check(db: Session, ctx: Any, storage_id: str) -> dict[str, Any]:
    """Read the storage (§4.4), fill the entry, and — with a default network profile —
    run the §5 cross checks against it; the results land on the entry."""
    row = _row(db, ctx.id, storage_id)
    if row is None:
        raise _not_found(storage_id)
    if row.kind == S3_BUCKET:
        return _run_bucket_check(db, ctx, row)
    try:
        facts = _reader(ctx, row.kind, row.access_point_arn)
    except (ClientError, BotoCoreError) as exc:
        raise _check_failed(exc, row.kind) from exc
    results = storage_results(facts)
    ap = facts.get("access_point") or {}
    fs = facts.get("file_system") or {}
    targets = list(facts.get("mount_targets") or [])
    row.posix_uid = ap.get("posix_uid")
    row.posix_gid = ap.get("posix_gid")
    row.root_directory = ap.get("root_directory")
    row.file_system_arn = fs.get("arn") or row.file_system_arn
    row.bucket_arn = fs.get("bucket_arn")
    row.mount_targets = targets
    row.mount_target_azs = sorted({t["availability_zone_id"] for t in targets
                                   if t.get("availability_zone_id")
                                   and _available(t.get("status"))})
    vpcs = sorted({t["vpc_id"] for t in targets if t.get("vpc_id")})
    row.vpc_id = vpcs[0] if len(vpcs) == 1 else None
    storage_status = status_of(results)
    if row.network_profile_id and storage_status != "blocked":
        network = _network_facts(db, ctx, {"profile_id": row.network_profile_id})
        if network is not None and not network.get("missing"):
            _read_network(ctx, network, sorted({g for t in targets
                                                for g in t.get("security_groups") or []}))
            entry = {**to_dict(row), "status": storage_status}
            cross = [r for r in evaluate_mount(entry, network, mode=row.max_mode)
                     if r["check"] != "storage.status"]
            results.extend({**r, "profile_id": row.network_profile_id} for r in cross)
    row.check_results = results
    row.status = status_of(results)
    row.checked_at = _now()
    row.updated_at = row.checked_at
    db.commit()
    return get_entry(db, ctx.id, storage_id, None, see_all=True)


def _run_bucket_check(db: Session, ctx: Any, row: StorageMount) -> dict[str, Any]:
    """`run_check` of an S3 bucket entry: the bucket's reachability, Region, encryption and
    Block Public Access; with a default network profile, the VPC's route to S3."""
    try:
        facts = _bucket_reader(ctx, row.bucket or "", row.bucket_owner)
    except (ClientError, BotoCoreError) as exc:
        raise _bucket_check_failed(exc) from exc
    results = bucket_results(facts, row, ctx.region)
    row.encryption = facts.get("encryption")
    row.kms_key_arn = facts.get("kms_key_arn")
    storage_status = status_of(results)
    if row.network_profile_id and storage_status != "blocked":
        network = _network_facts(db, ctx, {"profile_id": row.network_profile_id})
        if network is not None and not network.get("missing"):
            entry = {**to_dict(row), "status": storage_status}
            results.extend({**r, "profile_id": row.network_profile_id}
                           for r in _bucket_vpc_results(ctx, network, entry))
    row.check_results = results
    row.status = status_of(results)
    row.checked_at = _now()
    row.updated_at = row.checked_at
    db.commit()
    return get_entry(db, ctx.id, row.id, None, see_all=True)


def _gated(db: Session, workspace_id: str) -> bool:
    from app.dlc.releases import release_mode
    from app.models.ledger import Workspace

    return release_mode(db.get(Workspace, workspace_id)) == "gated"


def preflight_spec(db: Session, ctx: Any, spec: dict[str, Any],
                   project_id: str | None) -> dict[str, Any]:
    """§5 for one spec: every catalog mount × the spec's network. An entry the project
    may not use answers 404 (as everywhere else); `{status, results}` otherwise —
    `status` `ok` when the spec mounts nothing from the catalog."""
    mounts = spec_mounts(spec)
    if not mounts:
        return {"status": "ok", "results": []}
    network = _network_facts(db, ctx, spec.get("network"))
    results: list[dict[str, Any]] = []
    entries = []
    for mount in mounts:
        row = _visible(db, ctx.id, mount["storage_id"], project_id,
                       see_all=project_id is None)
        entries.append((mount, to_dict(row)))
    nfs = [(m, e) for m, e in entries if e["kind"] != S3_BUCKET]
    buckets = [(m, e) for m, e in entries if e["kind"] == S3_BUCKET]
    # S3 bucket mounts ride no NFS and work on PUBLIC: the entry, the method, the platform
    platform = (spec.get("runtime_config") or {}).get("platform_version")
    for mount, entry in buckets:
        results.extend(evaluate_bucket_mount(
            entry, mode=mount.get("mode") or "ro", method=str(spec.get("method") or ""),
            platform_version=platform))
    if network is None:
        results.extend({**_result("storage.network", "block", "S3 Files / EFS mounts need the "
                                  "agent in a VPC — choose a network profile"),
                        "storage_id": m["storage_id"]} for m, _e in nfs)
        return {"status": status_of(results), "results": results}
    if network.get("missing"):
        results.append(_result("storage.network", "block",
                               f"network profile '{network['missing']}' not found"))
        return {"status": "blocked", "results": results}
    if nfs:
        mt_groups = sorted({g for _m, e in nfs for t in e["mount_targets"]
                            for g in t.get("security_groups") or []})
        _read_network(ctx, network, mt_groups)
        byoc = spec.get("byoc") or {}
        gated = _gated(db, ctx.id)
        for mount, entry in nfs:
            results.extend(evaluate_mount(
                entry, network, mode=mount.get("mode") or "ro",
                method=str(spec.get("method") or ""),
                byoc_image=spec.get("method") == "byoc"
                and byoc.get("artifact_kind") == "container_image",
                gated=gated))
    for _mount, entry in buckets:
        results.extend(_bucket_vpc_results(ctx, network, entry))
    return {"status": status_of(results), "results": results}


def assert_preflight(db: Session, ctx: Any, spec: dict[str, Any],
                     project_id: str | None) -> dict[str, Any]:
    """`preflight_spec`, refusing a blocking result: 409 `storage.preflight_failed`
    (the deploy job never starts)."""
    report = preflight_spec(db, ctx, spec, project_id)
    blocking = [r for r in report["results"] if r.get("severity") == "block"]
    if blocking:
        raise AppError("storage.preflight_failed",
                       "storage preflight failed: " + "; ".join(r["message"] for r in blocking[:3]),
                       {"results": report["results"]}, status_code=409)
    return report


# ── agent specs ───────────────────────────────────────────────────────────────


def usable_entry(db: Session, workspace_id: str, project_id: str | None,
                 storage_id: str, mode: str = "ro") -> StorageMount:
    """The entry a spec in `project_id` may mount: allowed (else 404), not blocked by
    its last check (409 `storage.blocked`), and `mode` within its `max_mode` (400
    `storage.mode_exceeds_max`)."""
    row = _visible(db, workspace_id, storage_id, project_id, see_all=project_id is None)
    if row.status == "blocked":
        raise AppError("storage.blocked",
                       f"storage '{row.name}' failed its check — fix it, re-run the check, "
                       "or mount another storage", {"storage_id": storage_id}, status_code=409)
    if _MODE_RANK.get(mode, 0) > _MODE_RANK.get(row.max_mode, 0):
        raise AppError("storage.mode_exceeds_max",
                       f"storage '{row.name}' may be mounted {row.max_mode} at most",
                       {"storage_id": storage_id, "max_mode": row.max_mode}, status_code=400)
    return row


_STAMPED = ("access_point_arn", "bucket", "prefix", "scope", "bucket_owner", "bucket_region",
            "kms_key_arn")


def stamp_mount(mount: dict[str, Any], row: StorageMount) -> dict[str, Any]:
    """A catalog reference in stored form: whatever the client (or an earlier stamp of
    another kind) put in the entry-owned fields is replaced by the entry's own."""
    base = {k: v for k, v in mount.items() if k not in _STAMPED}
    base["mode"] = mount.get("mode") or "ro"
    base["mount_path"] = mount.get("mount_path") or f"/mnt/{row.id}"
    base["kind"] = row.kind
    if row.kind == S3_BUCKET:
        return {**base, "access_point_arn": None, "bucket": row.bucket,
                "prefix": row.prefix or "", "scope": row.scope,
                "bucket_owner": row.bucket_owner, "bucket_region": row.bucket_region,
                "kms_key_arn": row.kms_key_arn}
    return {**base, "access_point_arn": row.access_point_arn}


def assert_bucket_platform(spec: dict[str, Any], *, creating: bool) -> None:
    """The gate of a spec that mounts an S3 bucket (probes P2, P3, 2026-11-23): the
    Harness is refused (`storage.method_unsupported`, data-driven by
    `BUCKET_MOUNT_UNSUPPORTED`) and a Runtime needs platform V2 (`storage.requires_v2`;
    V1 is refused by AWS at create). A new agent's absent `runtime_config` is V2 (the
    pipeline's new-agent default); an existing agent's absent one is its live platform,
    which this save cannot read — set V2 explicitly."""
    method = str(spec.get("method") or "")
    if method in BUCKET_MOUNT_UNSUPPORTED:
        raise AppError("storage.method_unsupported",
                       f"S3 bucket storage cannot be mounted by a {method} agent: "
                       f"{BUCKET_MOUNT_UNSUPPORTED[method]}",
                       {"method": method}, status_code=422)
    from app.schemas.runtime_config import RUNTIME_CONFIG_METHODS

    version = (spec.get("runtime_config") or {}).get("platform_version")
    if version == "V2" or (version is None and creating and method in RUNTIME_CONFIG_METHODS):
        return
    raise AppError("storage.requires_v2",
                   "S3 bucket storage needs Runtime platform version V2 — switch this "
                   "agent to V2 in its Runtime settings",
                   {"platform_version": version}, status_code=422)


def normalize_spec_storage(db: Session, workspace_id: str, project_id: str | None,
                           spec: dict[str, Any], *, creating: bool = False) -> dict[str, Any]:
    """An incoming spec's catalog mounts in stored form (§4.2): each entry usable by
    the project, and stamped with the entry's kind and access point ARN (or bucket
    fields) — whatever the client sent there is replaced. ``creating`` is a new agent
    (its Runtime platform defaults to V2). Returns a new dict; specs without catalog
    mounts come back unchanged."""
    if not spec_mounts(spec):
        return spec
    fs = dict(spec.get("filesystem") or {})
    stamped = []
    for mount in fs.get("mounts") or []:
        row = usable_entry(db, workspace_id, project_id, mount["storage_id"],
                           mount.get("mode") or "ro")
        stamped.append(stamp_mount(mount, row))
    if any(m["kind"] == S3_BUCKET for m in stamped):
        assert_bucket_platform(spec, creating=creating)
        grants = bucket_grants([m for m in stamped if m["kind"] == S3_BUCKET],
                               agent_id="0" * 32)
        size = len(json.dumps(bucket_iam_statements(grants), separators=(",", ":")))
        if size > MAX_INLINE_POLICY_CHARS:
            raise AppError("storage.policy_too_large",
                           "the S3 bucket mounts need more IAM policy than a role may carry "
                           f"({size} of {MAX_INLINE_POLICY_CHARS} characters) — mount fewer "
                           "buckets", {"size": size}, status_code=422)
    fs["mounts"] = stamped
    return {**spec, "filesystem": fs}


def normalize_outputs(spec: dict[str, Any], resources: dict[str, Any] | None,
                      project_id: str | None, *, creating: bool = False) -> dict[str, Any]:
    """An incoming spec's session output folder (``filesystem.outputs``) in stored form:
    the workspace's agent-files bucket and the project segment of its prefix stamped by
    the server (whatever a client sent there is replaced). The same platform gate as an
    S3 bucket catalog mount (`assert_bucket_platform`: Runtime methods on V2) plus the
    bucket's presence — a workspace bootstrapped before the feature has none
    (`422 storage.agent_files_unavailable`). Returns a new dict; a spec without an
    output folder comes back unchanged."""
    fs = dict(spec.get("filesystem") or {})
    outputs = fs.get("outputs")
    if outputs is None:
        return spec
    assert_bucket_platform(spec, creating=creating)
    bucket = (resources or {}).get("agent_files_bucket")
    if not bucket:
        raise AppError("storage.agent_files_unavailable",
                       "this workspace has no agent-files bucket yet — an administrator "
                       "runs its bootstrap (storage stage) to create it",
                       status_code=422)
    fs["outputs"] = {"mount_path": outputs.get("mount_path") or OUTPUTS_DEFAULT_PATH,
                     "bucket": bucket, "project_key": project_id or OUTPUTS_SHARED_KEY}
    return {**spec, "filesystem": fs}


def restamp_outputs(spec: dict[str, Any], resources: dict[str, Any] | None,
                    project_id: str | None) -> bool:
    """Re-stamp a promoted spec's output folder from the target workspace, in place:
    its bucket, and the target's project segment. ``False`` when the target has no
    agent-files bucket (the caller refuses the release); ``True`` otherwise, also for a
    spec without an output folder."""
    outputs = (spec.get("filesystem") or {}).get("outputs")
    if outputs is None:
        return True
    bucket = (resources or {}).get("agent_files_bucket")
    if not bucket:
        return False
    outputs["bucket"] = bucket
    outputs["project_key"] = project_id or OUTPUTS_SHARED_KEY
    return True


def restamp_spec(db: Session, workspace_id: str, spec: dict[str, Any],
                 project_id: str | None = None, *,
                 mismatched: list[str] | None = None) -> list[str]:
    """Re-stamp a (promoted, already id-mapped) spec's catalog mounts from
    `workspace_id`'s entries, in place. Returns the storage ids with no live entry the
    project may use there (the caller refuses the release on any); ``mismatched``, when
    given, collects the ids whose entry there is of another kind than the source's."""
    missing: list[str] = []
    for mount in spec_mounts(spec):
        row = _row(db, workspace_id, mount["storage_id"])
        if row is None or (project_id is not None and
                           db.get(ProjectStorageMount, (project_id, row.pk)) is None):
            missing.append(mount["storage_id"])
            continue
        if mismatched is not None and mount.get("kind") and mount["kind"] != row.kind:
            mismatched.append(mount["storage_id"])
        stamped = stamp_mount(mount, row)
        mount.clear()
        mount.update(stamped)
    return missing


def filesystem_entries(spec: Any, agent_id: str | None = None) -> list[dict[str, Any]]:
    """The AgentCore `filesystemConfigurations` entries of a spec's resolved catalog
    mounts — what `deployer/filesystem.filesystem_configurations` appends after the
    inline ones. ``agent_id`` is the agent the spec belongs to: a per-session S3 bucket
    mount's prefix is ``<entry prefix><agent_id>/{runtimeSessionId}/``, so rendering one
    without it raises ``ValueError`` rather than send a wrong prefix."""
    out: list[dict[str, Any]] = []
    for kind, mount in spec.filesystem.catalog_mounts():
        if kind == S3_BUCKET:
            member: dict[str, Any] = {"bucket": mount.bucket, "mountPath": mount.mount_path}
            prefix = mounted_prefix(mount.prefix, mount.scope, agent_id)
            if prefix:
                member["prefix"] = prefix
            if mount.bucket_owner:
                member["s3BucketOwner"] = mount.bucket_owner
            if mount.bucket_region:
                member["s3BucketRegion"] = mount.bucket_region
            out.append({S3_BUCKET_KIND: member})
            continue
        key = "s3FilesAccessPoint" if kind == "s3files" else "efsAccessPoint"
        out.append({key: {"accessPointArn": mount.access_point_arn,
                          "mountPath": mount.mount_path}})
    return out


# ── S3 bucket IAM (probes P4, P4b) ────────────────────────────────────────────

_S3_READ = ["s3:GetObject"]
_S3_WRITE = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:DeleteObject"]


def _one_or_many(values: list[str]) -> str | list[str]:
    return values[0] if len(values) == 1 else values


def bucket_grants(mounts: list[dict[str, Any]], agent_id: str) -> list[dict[str, Any]]:
    """``[{bucket, prefix, mode, kms_key_arn}]`` of stamped s3bucket mount dicts: the
    prefix is the one AgentCore mounts (``{runtimeSessionId}`` token included)."""
    return [{"bucket": m["bucket"],
             "prefix": mounted_prefix(m.get("prefix"), m.get("scope"), agent_id),
             "mode": m.get("mode") or "ro", "kms_key_arn": m.get("kms_key_arn")}
            for m in mounts]


def bucket_iam_statements(grants: list[dict[str, Any]],
                          partition: str = "aws") -> list[dict[str, Any]]:
    """The execution role statements of S3 bucket mounts (shape live-verified, probe P4):

    * ``s3:ListBucket`` on the bucket, ``s3:prefix`` ∈ [``<P>*``, ``<P>`` without its
      slash] — for a per-session mount ``P`` carries ``${aws:AgentCoreRuntimeSourceSid}``
      where the session id goes, so a session lists only its own folder; a whole-bucket
      mount has no condition;
    * object actions on ``<bucket>/<P>*`` — ``GetObject`` read-only, plus ``PutObject``,
      ``AbortMultipartUpload`` and ``DeleteObject`` read-write;
    * ``kms:Decrypt`` (and ``kms:GenerateDataKey`` read-write) on the bucket's KMS key.

    Grouped by bucket (list) and by (bucket, mode) (objects): the output stays well under
    an inline policy's size."""
    listing: dict[str, list[str] | None] = {}
    objects: dict[tuple[str, str], set[str]] = {}
    keys: dict[str, set[str]] = {"ro": set(), "rw": set()}
    for grant in grants:
        bucket, mode = grant["bucket"], grant["mode"]
        prefix = iam_prefix(grant["prefix"] or "")
        arn = f"arn:{partition}:s3:::{bucket}"
        objects.setdefault((bucket, mode), set()).add(f"{arn}/{prefix}*")
        if not prefix:
            listing[bucket] = None
        elif listing.get(bucket, []) is not None:
            known = listing.setdefault(bucket, [])
            known.extend(p for p in (f"{prefix}*", prefix.rstrip("/")) if p not in known)
        if grant.get("kms_key_arn"):
            keys[mode].add(grant["kms_key_arn"])
    statements: list[dict[str, Any]] = []
    for bucket in sorted(listing):
        statement: dict[str, Any] = {"Effect": "Allow", "Action": "s3:ListBucket",
                                     "Resource": f"arn:{partition}:s3:::{bucket}"}
        prefixes = listing[bucket]
        if prefixes:
            statement["Condition"] = {"StringLike": {"s3:prefix": prefixes}}
        statements.append(statement)
    for (_bucket, mode), resources in sorted(objects.items()):
        statements.append({"Effect": "Allow",
                           "Action": list(_S3_WRITE if mode == "rw" else _S3_READ),
                           "Resource": _one_or_many(sorted(resources))})
    rw_keys = keys["rw"]
    for mode, arns in (("rw", rw_keys), ("ro", keys["ro"] - rw_keys)):
        if arns:
            statements.append({
                "Effect": "Allow",
                "Action": ["kms:Decrypt", "kms:GenerateDataKey"] if mode == "rw"
                else ["kms:Decrypt"],
                "Resource": _one_or_many(sorted(arns))})
    return statements


def bucket_iam_preview(row: StorageMount, partition: str = "aws") -> list[dict[str, Any]]:
    """What an agent mounting the entry (read-write when it may be) gets in its role
    policy, for the console: ``<agent_id>`` stands for the agent."""
    grants = bucket_grants([{"bucket": row.bucket, "prefix": row.prefix, "scope": row.scope,
                             "mode": row.max_mode, "kms_key_arn": row.kms_key_arn}],
                           agent_id="<agent_id>")
    return bucket_iam_statements(grants, partition)


def bucket_policy_preview(db: Session, row: StorageMount) -> dict[str, Any]:
    """The bucket policy an administrator may apply (the platform never edits a bucket
    it did not make): allow the workspace's agent roles the same access (what a
    cross-account bucket needs), and for a per-session entry the verified confinement
    (probe P4b) — a request made from inside a runtime session reaches only
    ``<prefix>*/<its own session id>/``; requests without the key (the backend) are
    untouched."""
    from app.core.regions import partition_for_region
    from app.models.ledger import Workspace

    workspace = db.get(Workspace, row.workspace_id) if row.workspace_id else None
    part = partition_for_region(workspace.region) if workspace is not None else "aws"
    account = workspace.account_id if workspace is not None else "<workspace-account-id>"
    bucket_arn = f"arn:{part}:s3:::{row.bucket}"
    prefix = row.prefix or ""
    roles = {"ArnLike": {"aws:PrincipalArn": f"arn:{part}:iam::{account}:role/launchpad-agent-*"}}
    write = row.max_mode == "rw"
    allow_list: dict[str, Any] = {
        "Sid": "LaunchpadAgentsList", "Effect": "Allow",
        "Principal": {"AWS": f"arn:{part}:iam::{account}:root"},
        "Action": "s3:ListBucket", "Resource": bucket_arn, "Condition": dict(roles)}
    if prefix:
        allow_list["Condition"] = {**roles, "StringLike": {
            "s3:prefix": [f"{prefix}*", prefix.rstrip("/")]}}
    statements: list[dict[str, Any]] = [
        allow_list,
        {"Sid": "LaunchpadAgentsObjects", "Effect": "Allow",
         "Principal": {"AWS": f"arn:{part}:iam::{account}:root"},
         "Action": list(_S3_WRITE if write else _S3_READ),
         "Resource": f"{bucket_arn}/{prefix}*", "Condition": dict(roles)},
    ]
    if row.scope == SCOPE_PER_SESSION:
        sid = iam_prefix("{runtimeSessionId}")
        scope_condition = {**roles, "Null": {"aws:AgentCoreRuntimeSourceSid": "false"}}
        statements += [
            {"Sid": "ConfineObjectsToOwnSession", "Effect": "Deny", "Principal": "*",
             "Action": list(_S3_WRITE), "NotResource": f"{bucket_arn}/{prefix}*/{sid}/*",
             "Condition": dict(scope_condition)},
            {"Sid": "ConfineListToOwnSession", "Effect": "Deny", "Principal": "*",
             "Action": "s3:ListBucket", "Resource": bucket_arn,
             "Condition": {**scope_condition, "StringNotLike": {
                 "s3:prefix": [f"{prefix}*/{sid}/*", f"{prefix}*/{sid}"]}}},
        ]
    return {"Version": "2012-10-17", "Statement": statements}


def storage_iam_statements(spec: Any, resources: dict[str, dict[str, Any]] | None = None,
                           agent_id: str | None = None, partition: str = "aws",
                           ) -> list[dict[str, Any]]:
    """The execution role's mount statements (§4.3), grouped by mode.

    Inline mounts (`s3_files` / `efs`) are read-write, as they have always been;
    catalog mounts carry their own `mode`. ``resources`` (storage id → ``{kind,
    access_point_arn}``) resolves catalog references not yet stamped; a stamped
    reference needs none. For a spec with inline mounts only the output equals
    `agent_iam.fs_policy_document`'s statements, so that function may delegate here.

    Shape (probed 2026-07-13, `agent_iam.fs_policy_document`): ClientMount/ClientWrite
    on the file system conditioned on `s3files:AccessPointArn`; GetAccessPoint on the
    access points, unconditioned; ListMountTargets on the file systems. EFS: Resource
    `*` conditioned on `elasticfilesystem:AccessPointArn`.

    S3 bucket mounts (`bucket_iam_statements`) follow the NFS ones. Their prefixes name the
    agent, so ``agent_id`` is required for a per-session one (``ValueError`` otherwise).
    """
    resources = resources or {}
    groups: dict[str, dict[str, list[str]]] = {
        kind: {"rw": [], "ro": []} for kind in ("s3files", "efs")
    }
    for m in spec.filesystem.s3_files:
        groups["s3files"]["rw"].append(m.access_point_arn)
    for m in spec.filesystem.efs:
        groups["efs"]["rw"].append(m.access_point_arn)
    for m in spec.filesystem.mounts:
        kind, arn = m.kind, m.access_point_arn
        if not (kind and arn):
            known = resources.get(m.storage_id) or {}
            kind, arn = known.get("kind"), known.get("access_point_arn")
        if kind in groups and arn:
            groups[kind][m.mode].append(arn)
    statements: list[dict[str, Any]] = []
    s3 = groups["s3files"]
    s3_all = [*s3["rw"], *(a for a in s3["ro"] if a not in s3["rw"])]
    if s3_all:
        for mode, actions in (("rw", ["s3files:ClientMount", "s3files:ClientWrite"]),
                              ("ro", ["s3files:ClientMount"])):
            arns = [a for a in dict.fromkeys(s3[mode]) if mode == "rw" or a not in s3["rw"]]
            if not arns:
                continue
            statements.append({
                "Effect": "Allow", "Action": actions,
                "Resource": sorted({a.split("/access-point/")[0] for a in arns}),
                "Condition": {"ArnEquals": {"s3files:AccessPointArn": arns}},
            })
        fs_arns = sorted({a.split("/access-point/")[0] for a in s3_all})
        statements.append({"Effect": "Allow", "Action": ["s3files:GetAccessPoint"],
                           "Resource": s3_all})
        statements.append({"Effect": "Allow", "Action": ["s3files:ListMountTargets"],
                           "Resource": fs_arns})
    efs = groups["efs"]
    for mode, actions in (("rw", ["elasticfilesystem:ClientMount",
                                  "elasticfilesystem:ClientWrite"]),
                          ("ro", ["elasticfilesystem:ClientMount"])):
        arns = [a for a in dict.fromkeys(efs[mode]) if mode == "rw" or a not in efs["rw"]]
        if arns:
            statements.append({
                "Effect": "Allow", "Action": actions, "Resource": "*",
                "Condition": {"ArnEquals": {"elasticfilesystem:AccessPointArn": arns}},
            })
    buckets = [m for kind, m in spec.filesystem.catalog_mounts() if kind == S3_BUCKET]
    grants = bucket_grants([m.model_dump() for m in buckets], agent_id or "")
    outputs = spec.filesystem.outputs
    if outputs is not None and outputs.resolved:
        # the session output folder: read-write, this agent's own sessions only
        grants.append({"bucket": outputs.bucket,
                       "prefix": outputs_prefix(outputs.project_key, agent_id or ""),
                       "mode": "rw", "kms_key_arn": None})
    if grants:
        statements.extend(bucket_iam_statements(grants, partition))
    return statements


# ── an agent's storage (§10 "storage" card) ───────────────────────────────────


def live_mounts(control: Any, db: Session, agent: Agent) -> dict[str, Any]:
    """The S3 Files / EFS / S3 bucket mounts the agent's Runtime or Harness carries on
    AWS (a member of another kind is not listed), each marked ``platform`` (a mount
    path a Launchpad publish set) or ``external`` (added outside Launchpad, e.g. in the
    AWS console, or on an imported agent) and matched to a catalog entry by access
    point ARN. A re-publish keeps the external ones (``deployer/filesystem.merge_live``).
    An ``s3bucket`` entry carries ``bucket``, ``prefix``, ``bucket_owner`` and
    ``bucket_region`` instead of an access point (the bucket fields are ``None`` on the
    others). Read-only."""
    from app.deployer.filesystem import mount_path
    from app.deployer.mounts import manifest_paths, published_mount_paths
    from app.services.agent_versions import resource_kind
    from app.services.agentcore import harness as harness_api
    from app.services.agentcore import runtime as runtime_api

    kind = resource_kind(agent)
    if kind is None or not agent.resource_id or agent.status == "deleted":
        return {"status": "no_resource", "entries": []}
    if kind == "harness":
        live = harness_api.get_harness(control, agent.resource_id)
        env = (live.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}
        entries = env.get("filesystemConfigurations") or []
    else:
        entries = runtime_api.get_runtime(control, agent.resource_id).get(
            "filesystemConfigurations") or []
    owned = manifest_paths(agent.spec) | published_mount_paths(db, agent.id)
    rows = list(db.scalars(select(StorageMount).where(
        StorageMount.workspace_id == (agent.workspace_id or ""),
        StorageMount.archived_at.is_(None))))
    by_arn = {r.access_point_arn: r for r in rows if r.kind != S3_BUCKET}
    # an S3 bucket entry has no access point: it is the one whose bucket and mounted
    # prefix (per-session ones carry this agent's id) the live entry carries
    by_mount = {(r.bucket, mounted_prefix(r.prefix, r.scope, agent.id)): r
                for r in rows if r.kind == S3_BUCKET}
    out = []
    for fs in entries:
        fs_kind = member_kind(fs)
        label = LIVE_KIND.get(fs_kind)
        if label is None:  # session storage, or a member this card does not model
            continue
        member = fs[fs_kind]
        arn = str(member.get("accessPointArn") or "")
        row = by_arn.get(arn) if arn else None
        if fs_kind == S3_BUCKET_KIND:
            row = by_mount.get((member.get("bucket"), str(member.get("prefix") or "")))
        out.append({
            "kind": label,
            "mount_path": mount_path(fs),
            "access_point_arn": arn or None,
            "bucket": member.get("bucket") if fs_kind == S3_BUCKET_KIND else None,
            "prefix": member.get("prefix") if fs_kind == S3_BUCKET_KIND else None,
            "bucket_owner": member.get("s3BucketOwner") if fs_kind == S3_BUCKET_KIND else None,
            "bucket_region": member.get("s3BucketRegion") if fs_kind == S3_BUCKET_KIND else None,
            "origin": "platform" if mount_path(fs) in owned else "external",
            "storage_id": row.id if row is not None else None,
            "storage_name": row.name if row is not None else None,
        })
    return {"status": "ok", "kind": kind, "entries": out}


def agent_storage(db: Session, agent: Agent) -> dict[str, Any]:
    """The card body: each catalog mount with its entry's state, plus the inline
    (legacy) mounts as they are."""
    workspace_id = agent.workspace_id or ""
    spec = agent.spec or {}
    mounts = []
    for mount in spec_mounts(spec):
        row = _row(db, workspace_id, mount["storage_id"], archived=True)
        entry = None
        if row is not None:
            results = row.check_results or []
            entry = {"name": row.name, "kind": row.kind, "status": row.status,
                     "bucket": row.bucket, "prefix": row.prefix, "scope": row.scope,
                     "max_mode": row.max_mode, "data_class": row.data_class,
                     "archived": row.archived_at is not None,
                     "checked_at": row.checked_at.isoformat() if row.checked_at else None,
                     "blocking": sum(1 for r in results if r.get("severity") == "block"),
                     "warnings": sum(1 for r in results if r.get("severity") == "warn")}
        mounts.append({"storage_id": mount["storage_id"],
                       "mount_path": mount.get("mount_path") or f"/mnt/{mount['storage_id']}",
                       "mode": mount.get("mode") or "ro", "entry": entry,
                       "missing": row is None})
    inline = []
    fs = spec.get("filesystem") or {}
    for key, kind in (("s3_files", "s3files"), ("efs", "efs")):
        for m in fs.get(key) or []:
            inline.append({"kind": kind, "access_point_arn": (m or {}).get("access_point_arn"),
                           "mount_path": (m or {}).get("mount_path"), "mode": "rw"})
    session = fs.get("session_storage", {"mount_path": "/mnt/workspace"})
    outputs = fs.get("outputs")
    return {"agent_id": agent.id, "version": agent.version, "mounts": mounts,
            "inline": inline,
            "session_storage": (session or {}).get("mount_path") if session else None,
            # the session output folder (null = off); its objects expire after 30 days
            "outputs": ({"mount_path": outputs.get("mount_path") or OUTPUTS_DEFAULT_PATH,
                         "bucket": outputs.get("bucket")} if outputs is not None else None)}


# ── isolated projects' permissions boundary ───────────────────────────────────


def boundary_statements(inp: Any) -> list[dict[str, Any]]:
    """`project_iam.EXTRA_BOUNDARY_STATEMENT_BUILDERS`: an isolated project's boundary
    allows the S3 bucket entries allowed to the project in the boundary's workspace. The
    boundary applies to the mount itself (probe P9: without S3 in it the mount fails
    ``424 S3 mount failed: access denied``), so it lists each entry's bucket and prefix
    (prefix-wide: the per-agent role policy is what narrows a per-session mount to its
    own session) with the actions of the entry's ``max_mode``. A revoked entry leaves
    with the next boundary render (deploy)."""
    from app.core.db import SessionLocal
    from app.models.ledger import Workspace

    db = SessionLocal()
    try:
        with project_context(None):
            workspaces = [w for (w,) in db.execute(select(Workspace.id).where(
                Workspace.account_id == inp.account_id, Workspace.region == inp.region))]
            if not workspaces:
                return []
            rows = list(db.scalars(
                select(StorageMount).join(
                    ProjectStorageMount, ProjectStorageMount.mount_pk == StorageMount.pk)
                .where(StorageMount.workspace_id.in_(workspaces),
                       StorageMount.kind == S3_BUCKET, StorageMount.archived_at.is_(None),
                       ProjectStorageMount.project_id == inp.project_id)
                .order_by(StorageMount.bucket, StorageMount.prefix)))
    except Exception:  # noqa: BLE001 — no ledger (a pure document test): nothing allowed
        logger.debug("storage entries for the boundary of %s unreadable", inp.project_id,
                     exc_info=True)
        return []
    finally:
        db.close()
    if not rows:
        return []
    grants = [{"bucket": r.bucket, "prefix": r.prefix or "", "mode": r.max_mode,
               "kms_key_arn": r.kms_key_arn} for r in rows]
    # compact: one object statement for every bucket (read-write when any entry may
    # write); the bucket list statements and the KMS grants as `bucket_iam_statements`
    # builds them
    writes = any(g["mode"] == "rw" for g in grants)
    built = bucket_iam_statements([{**g, "mode": "rw" if writes else "ro"} for g in grants],
                                  inp.partition)

    def first(statement: dict[str, Any]) -> str:
        action = statement["Action"]
        return action if isinstance(action, str) else action[0]

    objects = sorted({r for st in built if first(st) == "s3:GetObject"
                      for r in ([st["Resource"]] if isinstance(st["Resource"], str)
                                else st["Resource"])})
    out = [st for st in built if first(st) == "s3:ListBucket"]
    out.append({"Effect": "Allow", "Action": list(_S3_WRITE if writes else _S3_READ),
                "Resource": objects})
    out.extend(st for st in built if first(st).startswith("kms:"))
    return [{"Sid": f"S3Mount{i}", **st} for i, st in enumerate(out)]
