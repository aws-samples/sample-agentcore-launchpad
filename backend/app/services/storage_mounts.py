"""The storage catalog (design storage-mounts §4–§6): S3 Files / EFS access points an
administrator registers per workspace, the per-project allowed set that decides who may
mount one, the checks, the agent-time preflight, and what an agent's spec and IAM role
get from a catalog reference.

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
import logging
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

logger = logging.getLogger(__name__)

NFS_PORT = 2049
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
    return to_dict(row, allowed=allowed,
                   projects=[b.project_id for b in _bindings_of(db, row)],
                   agents=usage(db, workspace_id, row.id).get(row.id, []))


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
    arn = fields["access_point_arn"].strip()
    kind = kind_of_arn(arn)
    if kind is None:
        raise AppError("storage.invalid_access_point",
                       f"'{arn}' is not an S3 Files or EFS access point ARN",
                       {"access_point_arn": arn}, status_code=422)
    _assert_profile(db, workspace_id, fields.get("network_profile_id"))
    row = StorageMount(
        id=storage_id, workspace_id=workspace_id, name=fields.get("name") or storage_id,
        kind=kind, access_point_arn=arn, file_system_arn=file_system_arn_of(kind, arn),
        network_profile_id=fields.get("network_profile_id") or None,
        max_mode=fields.get("max_mode") or "ro",
        data_class=fields.get("data_class") or "reference",
        origin=origin, status="unchecked", created_by=actor,
    )
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
    if network is None:
        results.extend({**_result("storage.network", "block", "S3 Files / EFS mounts need the "
                                  "agent in a VPC — choose a network profile"),
                        "storage_id": m["storage_id"]} for m, _e in entries)
        return {"status": "blocked", "results": results}
    if network.get("missing"):
        results.append(_result("storage.network", "block",
                               f"network profile '{network['missing']}' not found"))
        return {"status": "blocked", "results": results}
    mt_groups = sorted({g for _m, e in entries for t in e["mount_targets"]
                        for g in t.get("security_groups") or []})
    _read_network(ctx, network, mt_groups)
    byoc = spec.get("byoc") or {}
    gated = _gated(db, ctx.id)
    for mount, entry in entries:
        results.extend(evaluate_mount(
            entry, network, mode=mount.get("mode") or "ro", method=str(spec.get("method") or ""),
            byoc_image=spec.get("method") == "byoc"
            and byoc.get("artifact_kind") == "container_image",
            gated=gated))
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


def normalize_spec_storage(db: Session, workspace_id: str, project_id: str | None,
                           spec: dict[str, Any]) -> dict[str, Any]:
    """An incoming spec's catalog mounts in stored form (§4.2): each entry usable by
    the project, and stamped with the entry's kind and access point ARN — whatever the
    client sent there is replaced. Returns a new dict; specs without catalog mounts come
    back unchanged."""
    if not spec_mounts(spec):
        return spec
    fs = dict(spec.get("filesystem") or {})
    stamped = []
    for mount in fs.get("mounts") or []:
        row = usable_entry(db, workspace_id, project_id, mount["storage_id"],
                           mount.get("mode") or "ro")
        stamped.append({**mount, "mode": mount.get("mode") or "ro",
                        "mount_path": mount.get("mount_path") or f"/mnt/{row.id}",
                        "kind": row.kind, "access_point_arn": row.access_point_arn})
    fs["mounts"] = stamped
    return {**spec, "filesystem": fs}


def restamp_spec(db: Session, workspace_id: str, spec: dict[str, Any],
                 project_id: str | None = None) -> list[str]:
    """Re-stamp a (promoted, already id-mapped) spec's catalog mounts from
    `workspace_id`'s entries, in place. Returns the storage ids with no live entry the
    project may use there (the caller refuses the release on any)."""
    missing: list[str] = []
    for mount in spec_mounts(spec):
        row = _row(db, workspace_id, mount["storage_id"])
        if row is None or (project_id is not None and
                           db.get(ProjectStorageMount, (project_id, row.pk)) is None):
            missing.append(mount["storage_id"])
            continue
        mount["kind"], mount["access_point_arn"] = row.kind, row.access_point_arn
    return missing


def filesystem_entries(spec: Any) -> list[dict[str, Any]]:
    """The AgentCore `filesystemConfigurations` entries of a spec's resolved catalog
    mounts — what `deployer/filesystem.filesystem_configurations` appends after the
    inline ones."""
    out: list[dict[str, Any]] = []
    for kind, mount in spec.filesystem.catalog_mounts():
        key = "s3FilesAccessPoint" if kind == "s3files" else "efsAccessPoint"
        out.append({key: {"accessPointArn": mount.access_point_arn,
                          "mountPath": mount.mount_path}})
    return out


def storage_iam_statements(spec: Any, resources: dict[str, dict[str, Any]] | None = None,
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
    return statements


# ── an agent's storage (§10 "storage" card) ───────────────────────────────────


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
    return {"agent_id": agent.id, "version": agent.version, "mounts": mounts,
            "inline": inline,
            "session_storage": (session or {}).get("mount_path") if session else None}
