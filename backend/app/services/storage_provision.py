"""The console creates (and tears down) S3 Files storage (design storage-mounts §7).

`POST /api/storage-mounts/provision` queues a Job `storage_provision` that builds, in
the workspace's account and region, everything an agent needs to mount a new S3 file
system, and registers the result in the storage catalog with `origin="platform"`:

    validate → bucket → file-system → security-group → mount-targets → access-point
             → register

* **bucket**: a new versioned bucket `launchpad-s3files-<account>-<hash>` (S3 Files
  requires versioning), public access blocked, tagged. The platform never links a
  customer's existing bucket: the shared sync role reaches only this name prefix.
* **file-system**: `CreateFileSystem` on that bucket with the workspace's shared sync
  role (`resources.s3files_sync_role_arn`, made by `make bootstrap` / the workspace
  bootstrap `iam` stage), which S3 Files assumes to keep files and objects in step.
* **security-group**: `launchpad-mt-<id>` in the network profile's VPC, admitting
  TCP 2049 from the profile's security groups only. The profile's own groups are the
  customer's and stay untouched; when they do not allow 2049 out, the catalog check
  (and every agent's preflight) blocks and says so.
* **mount-targets**: one per Availability Zone of the profile's subnets.
* **access-point**: the POSIX identity and root directory the form names.

`POST /api/storage-mounts/{id}/teardown` queues the same job type with `op=delete`:

    validate → access-point → mount-targets → file-system → security-group → archive

It deletes what the create made and archives the catalog entry, refusing while an agent
mounts it. **The bucket is kept**, with every object S3 Files synchronized into it: the
platform does not delete customer data (design principle 4).

Every stage is idempotent (list-or-get, then create with a client token), records what
it made on `Job.payload["resources"]` the moment it exists, and a restart resumes the
job from its first unfinished stage (`deployer.pipeline.resume_pending_jobs`). A retry
after a failure (`POST …/provision` again with the same id) resumes the failed job.
The job is workspace infrastructure: it runs outside any project.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.core.project_scope import project_context
from app.models.ledger import Job
from app.services.workspace_iam import S3FILES_BUCKET_PREFIX

logger = logging.getLogger(__name__)

JOB_TYPE = "storage_provision"
OP_CREATE, OP_DELETE = "create", "delete"
CREATE_STAGES = ["validate", "bucket", "file-system", "security-group", "mount-targets",
                 "access-point", "register"]
DELETE_STAGES = ["validate", "access-point", "mount-targets", "file-system",
                 "security-group", "archive"]
ACTIVE = ("queued", "running")
NFS_PORT = 2049
SYNC_ROLE_KEY = "s3files_sync_role_arn"
MANAGED_TAG = "launchpad:managed"
STORAGE_TAG = "launchpad:storage"
WORKSPACE_TAG = "launchpad:workspace"

# how long a stage waits for S3 Files / EC2 to settle, and how often it looks;
# module-level so tests run without sleeping
POLL_S = 10.0
WAIT_S = 900.0

_LIVE: dict[str, threading.Thread] = {}
_LIVE_LOCK = threading.Lock()
_QUEUE_LOCK = threading.Lock()


class ProvisionError(RuntimeError):
    """A stage failed for a reason the administrator can act on."""


# ── names ────────────────────────────────────────────────────────────────────


def bucket_name(account_id: str, region: str, workspace_id: str, storage_id: str) -> str:
    """Deterministic (a resume finds the bucket again) and within S3's 63 characters:
    the region, workspace and id go into a short hash."""
    digest = hashlib.sha256(f"{region}:{workspace_id}:{storage_id}".encode()).hexdigest()[:10]
    return f"{S3FILES_BUCKET_PREFIX}{account_id}-{digest}"


def security_group_name(storage_id: str) -> str:
    return f"launchpad-mt-{storage_id}"


def access_point_name(storage_id: str) -> str:
    return f"launchpad-{storage_id}"


def _tags(workspace_id: str, storage_id: str) -> dict[str, str]:
    return {MANAGED_TAG: "true", STORAGE_TAG: storage_id, WORKSPACE_TAG: workspace_id}


def _client_token(job_id: str, step: str) -> str:
    return f"lp-{job_id[:16]}-{step}"[:64]


# ── job bookkeeping ──────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _stage_names(op: str) -> list[str]:
    return CREATE_STAGES if op == OP_CREATE else DELETE_STAGES


def _new_stages(op: str) -> list[dict[str, Any]]:
    return [{"name": name, "status": "pending", "detail": ""} for name in _stage_names(op)]


def _log(job: Job, stage: str, message: str, level: str = "info") -> None:
    line = json.dumps({"ts": _now_iso(), "stage": stage, "level": level, "msg": message},
                      ensure_ascii=False)
    job.log = (job.log + "\n" + line) if job.log else line


def _jobs(db: Session, workspace_id: str, storage_id: str | None = None) -> list[Job]:
    with project_context(None):
        stmt = select(Job).where(Job.type == JOB_TYPE, Job.workspace_id == workspace_id)
        rows = list(db.scalars(stmt.order_by(Job.created_at.desc())))
    if storage_id is not None:
        rows = [j for j in rows if (j.payload or {}).get("storage_id") == storage_id]
    return rows


def active_job(db: Session, workspace_id: str, storage_id: str) -> Job | None:
    return next((j for j in _jobs(db, workspace_id, storage_id) if j.status in ACTIVE), None)


def view(job: Job) -> dict[str, Any]:
    """The progress shape the storage page polls."""
    payload = job.payload or {}
    return {
        "job_id": job.id,
        "op": payload.get("op"),
        "storage_id": payload.get("storage_id"),
        "status": job.status,
        "done": job.status not in ACTIVE,
        "stages": list(payload.get("stages") or []),
        "resources": dict(payload.get("resources") or {}),
        # what the form asked for: the page's retry resubmits it (no secrets in it)
        "request": dict(payload.get("request") or {}),
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def list_jobs(db: Session, workspace_id: str, limit: int = 20) -> list[dict[str, Any]]:
    return [view(j) for j in _jobs(db, workspace_id)[:limit]]


def get_job(db: Session, workspace_id: str, job_id: str) -> dict[str, Any]:
    with project_context(None):
        job = db.get(Job, job_id)
    if job is None or job.type != JOB_TYPE or job.workspace_id != workspace_id:
        raise NotFoundError("storage.job_not_found", f"storage job '{job_id}' not found",
                            {"job_id": job_id})
    return view(job)


# ── queueing ─────────────────────────────────────────────────────────────────


def _sync_role(resources: dict[str, Any]) -> str:
    arn = str((resources or {}).get(SYNC_ROLE_KEY) or "")
    if not arn:
        raise AppError(
            "storage.sync_role_missing",
            "this workspace has no S3 Files sync role yet — re-run `make bootstrap` (the "
            "default workspace) or the workspace's bootstrap, then try again",
            {"resource": SYNC_ROLE_KEY}, status_code=409)
    return arn


def queue_create(db: Session, workspace: Any, request: dict[str, Any], actor: str
                 ) -> tuple[Job, bool]:
    """Queue (or resume) the creation of storage `request["id"]`. Returns
    ``(job, started_new)``; a retry with the same id resumes a failed or running job."""
    from app.models.network import NetworkProfile
    from app.services import storage_mounts

    storage_id = request["id"]
    with _QUEUE_LOCK:
        active = active_job(db, workspace.id, storage_id)
        if active is not None:
            return active, False
        if storage_mounts._row(db, workspace.id, storage_id) is not None:
            raise AppError("storage.id_exists",
                           f"storage '{storage_id}' is already registered in this workspace",
                           {"storage_id": storage_id}, status_code=409)
        profile_id = request["network_profile_id"]
        profile = db.get(NetworkProfile, profile_id)
        if profile is None or profile.workspace_id != workspace.id or profile.archived_at:
            raise NotFoundError("network_profile.not_found",
                                f"network profile '{profile_id}' not found",
                                {"profile_id": profile_id})
        _sync_role(workspace.resources)
        for project_id in request.get("project_ids") or []:
            from app.services.network_profiles import assert_project_in_workspace

            assert_project_in_workspace(db, workspace.id, project_id)
        failed = next((j for j in _jobs(db, workspace.id, storage_id)
                       if j.status == "failed" and (j.payload or {}).get("op") == OP_CREATE),
                      None)
        if failed is not None:
            # a retry continues where the failed run stopped; the form may change
            # only what no stage has used yet (the catalog fields)
            payload = dict(failed.payload or {})
            payload["request"] = {**payload.get("request", {}), **request}
            failed.payload = payload
            failed.status, failed.error = "queued", None
            _log(failed, "queue", f"retried by {actor}")
            db.commit()
            return failed, True
        with project_context(None):
            job = Job(workspace_id=workspace.id, type=JOB_TYPE, status="queued", project_id=None,
                      payload={"op": OP_CREATE, "workspace_id": workspace.id,
                               "storage_id": storage_id, "actor": actor, "request": request,
                               "stages": _new_stages(OP_CREATE), "resources": {}})
            db.add(job)
            db.commit()
            db.refresh(job)
        return job, True


def queue_delete(db: Session, workspace: Any, storage_id: str, confirm: str, actor: str
                 ) -> tuple[Job, bool]:
    """Queue the teardown of a platform-made entry. `confirm` must repeat its id."""
    from app.services import storage_mounts

    with _QUEUE_LOCK:
        active = active_job(db, workspace.id, storage_id)
        if active is not None:
            return active, False
        row = storage_mounts._row(db, workspace.id, storage_id)
        if row is None:
            raise storage_mounts._not_found(storage_id)
        if row.origin != "platform":
            raise AppError("storage.not_platform",
                           f"'{row.name}' was registered, not created by the console — the "
                           "platform never deletes registered storage; archive it instead",
                           {"storage_id": storage_id}, status_code=409)
        if confirm != row.id:
            raise AppError("storage.confirm_mismatch",
                           "type the storage id to confirm its deletion",
                           {"storage_id": storage_id}, status_code=422)
        storage_mounts.assert_unused(db, workspace.id, row)
        made = next((j for j in _jobs(db, workspace.id, storage_id)
                     if (j.payload or {}).get("op") == OP_CREATE
                     and j.status == "succeeded"), None)
        resources = dict((made.payload or {}).get("resources") or {}) if made else {}
        resources.update({"access_point_arn": row.access_point_arn,
                          "file_system_arn": row.file_system_arn})
        with project_context(None):
            job = Job(workspace_id=workspace.id, type=JOB_TYPE, status="queued", project_id=None,
                      payload={"op": OP_DELETE, "workspace_id": workspace.id,
                               "storage_id": storage_id, "actor": actor, "request": {},
                               "stages": _new_stages(OP_DELETE), "resources": resources})
            db.add(job)
            db.commit()
            db.refresh(job)
        return job, True


# ── AWS helpers ──────────────────────────────────────────────────────────────


def _code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", ""))
    return type(exc).__name__


def _gone(exc: Exception) -> bool:
    return _code(exc) in ("ResourceNotFoundException", "NotFoundException", "FileSystemNotFound",
                          "AccessPointNotFound", "MountTargetNotFound", "NoSuchBucket",
                          "InvalidGroup.NotFound", "InvalidGroupId.NotFound")


def _wait(what: str, probe: Callable[[], bool], timeout: float | None = None) -> None:
    deadline = time.monotonic() + (WAIT_S if timeout is None else timeout)
    while True:
        if probe():
            return
        if time.monotonic() >= deadline:
            raise ProvisionError(f"{what} did not settle within {int(WAIT_S)} s — retry later")
        time.sleep(POLL_S)


def _s3files_tags(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"key": k, "value": v} for k, v in tags.items()]


def _pages(call: Callable[..., dict[str, Any]], key: str, **kwargs: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while True:
        page = call(**kwargs)
        out.extend(page.get(key) or [])
        token = page.get("nextToken")
        if not token:
            return out
        kwargs["nextToken"] = token


# ── the run ──────────────────────────────────────────────────────────────────


class _Run:
    """One execution: the job's payload, the workspace and a `record` that persists a
    resource the moment it exists."""

    def __init__(self, db: Session, job_id: str, workspace: Any):
        self.db, self.job_id, self.workspace = db, job_id, workspace

    @property
    def job(self) -> Job:
        with project_context(None):
            return self.db.get(Job, self.job_id)

    @property
    def payload(self) -> dict[str, Any]:
        return dict(self.job.payload or {})

    @property
    def request(self) -> dict[str, Any]:
        return dict(self.payload.get("request") or {})

    @property
    def resources(self) -> dict[str, Any]:
        return dict(self.payload.get("resources") or {})

    @property
    def storage_id(self) -> str:
        return str(self.payload.get("storage_id"))

    @property
    def tags(self) -> dict[str, str]:
        return _tags(self.workspace.id, self.storage_id)

    def record(self, **values: Any) -> None:
        job = self.job
        payload = dict(job.payload or {})
        payload["resources"] = {**(payload.get("resources") or {}), **values}
        job.payload = payload
        self.db.commit()

    def log(self, stage: str, message: str, level: str = "info") -> None:
        job = self.job
        _log(job, stage, message, level)
        self.db.commit()

    def set_stage(self, stage: str, status: str, detail: str = "") -> None:
        job = self.job
        payload = dict(job.payload or {})
        stages = [dict(s) for s in payload.get("stages") or []]
        for entry in stages:
            if entry["name"] == stage:
                entry["status"] = status
                if detail:
                    entry["detail"] = detail
                if status == "running":
                    entry["started_at"] = _now_iso()
                elif status in ("succeeded", "failed"):
                    entry["ended_at"] = _now_iso()
        payload["stages"] = stages
        job.payload = payload
        self.db.commit()

    def s3files(self) -> Any:
        return self.workspace.client("s3files")

    def ec2(self) -> Any:
        return self.workspace.client("ec2")


# ── create stages ────────────────────────────────────────────────────────────


def _stage_validate_create(run: _Run) -> str:
    from app.models.network import NetworkProfile

    request = run.request
    profile = run.db.get(NetworkProfile, request["network_profile_id"])
    if profile is None or profile.archived_at:
        raise ProvisionError(f"network profile '{request['network_profile_id']}' is gone")
    role_arn = _sync_role(run.workspace.resources)
    subnets = run.ec2().describe_subnets(SubnetIds=list(profile.subnet_ids or []))["Subnets"]
    vpcs = {s["VpcId"] for s in subnets}
    if len(vpcs) != 1:
        raise ProvisionError("the network profile's subnets are not in one VPC")
    by_az: dict[str, str] = {}
    for subnet in sorted(subnets, key=lambda s: s["SubnetId"]):
        by_az.setdefault(subnet["AvailabilityZoneId"], subnet["SubnetId"])
    run.record(vpc_id=vpcs.pop(), subnets=[{"subnet_id": sid, "availability_zone_id": az}
                                           for az, sid in sorted(by_az.items())],
               agent_security_groups=list(profile.security_group_ids or []),
               sync_role_arn=role_arn)
    return f"{len(by_az)} Availability Zone(s) in {run.resources['vpc_id']}"


def _bucket_tags(s3: Any, name: str) -> dict[str, str]:
    try:
        tags = s3.get_bucket_tagging(Bucket=name).get("TagSet") or []
    except ClientError as exc:
        if _code(exc) == "NoSuchTagSet":
            return {}
        raise
    return {t["Key"]: t["Value"] for t in tags}


def _stage_bucket(run: _Run) -> str:
    ws = run.workspace
    name = bucket_name(ws.account_id, ws.region, ws.id, run.storage_id)
    s3 = ws.client("s3")
    try:
        s3.head_bucket(Bucket=name, ExpectedBucketOwner=ws.account_id)
        exists = True
    except ClientError as exc:
        if _code(exc) not in ("404", "NoSuchBucket", "NotFound"):
            raise
        exists = False
    if exists:
        if _bucket_tags(s3, name).get(STORAGE_TAG) != run.storage_id:
            raise ProvisionError(f"bucket '{name}' exists but was not made for this storage")
        detail = f"adopted {name}"
    else:
        kwargs: dict[str, Any] = {"Bucket": name}
        if ws.region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": ws.region}
        s3.create_bucket(**kwargs)
        detail = f"created {name}"
    run.record(bucket=name, bucket_arn=f"arn:{ws.partition}:s3:::{name}")
    s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    # S3 Files requires a versioned bucket
    s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_tagging(Bucket=name, Tagging={
        "TagSet": [{"Key": k, "Value": v} for k, v in run.tags.items()]})
    return detail


def _file_system_status(client: Any, fs_id: str) -> str:
    return str(client.get_file_system(fileSystemId=fs_id).get("status") or "")


def _stage_file_system(run: _Run) -> str:
    client = run.s3files()
    res = run.resources
    fs_id = res.get("file_system_id")
    if not fs_id:
        existing = [f for f in _pages(client.list_file_systems, "fileSystems",
                                      bucket=res["bucket_arn"])
                    if f.get("status") not in ("deleting", "deleted")]
        if existing:
            fs = existing[0]
            detail = f"adopted {fs['fileSystemId']}"
        else:
            fs = client.create_file_system(
                bucket=res["bucket_arn"], roleArn=res["sync_role_arn"],
                clientToken=_client_token(run.job_id, "fs"),
                tags=_s3files_tags({"Name": f"launchpad-{run.storage_id}", **run.tags}))
            detail = f"created {fs['fileSystemId']}"
        fs_id = fs["fileSystemId"]
        run.record(file_system_id=fs_id, file_system_arn=fs["fileSystemArn"])
    else:
        detail = f"resumed {fs_id}"

    def available() -> bool:
        status = _file_system_status(client, fs_id)
        if status == "error":
            raise ProvisionError(f"file system {fs_id} failed to create")
        return status == "available"

    _wait(f"file system {fs_id}", available)
    return detail


def _stage_security_group(run: _Run) -> str:
    ec2 = run.ec2()
    res = run.resources
    name = security_group_name(run.storage_id)
    found = ec2.describe_security_groups(Filters=[
        {"Name": "vpc-id", "Values": [res["vpc_id"]]},
        {"Name": "group-name", "Values": [name]},
    ]).get("SecurityGroups") or []
    if found:
        if {t["Key"]: t["Value"] for t in found[0].get("Tags") or []}.get(STORAGE_TAG) \
                != run.storage_id:
            raise ProvisionError(f"security group '{name}' exists but was not made by the "
                                 "console for this storage")
        group_id, detail = found[0]["GroupId"], f"adopted {found[0]['GroupId']}"
    else:
        group_id = ec2.create_security_group(
            GroupName=name, VpcId=res["vpc_id"],
            Description=f"Launchpad S3 Files mount targets of storage {run.storage_id}",
            TagSpecifications=[{"ResourceType": "security-group", "Tags": [
                {"Key": "Name", "Value": name},
                *({"Key": k, "Value": v} for k, v in run.tags.items())]}],
        )["GroupId"]
        detail = f"created {group_id}"
    run.record(mount_target_security_group=group_id)
    sources = list(res.get("agent_security_groups") or [])
    try:
        ec2.authorize_security_group_ingress(GroupId=group_id, IpPermissions=[{
            "IpProtocol": "tcp", "FromPort": NFS_PORT, "ToPort": NFS_PORT,
            "UserIdGroupPairs": [{"GroupId": g, "Description": "Launchpad agents (NFS)"}
                                 for g in sources]}])
    except ClientError as exc:
        if _code(exc) != "InvalidPermission.Duplicate":
            raise
    return f"{detail}; TCP {NFS_PORT} from {', '.join(sources)}"


def _mount_targets(client: Any, fs_id: str) -> list[dict[str, Any]]:
    return _pages(client.list_mount_targets, "mountTargets", fileSystemId=fs_id)


def _stage_mount_targets(run: _Run) -> str:
    client = run.s3files()
    res = run.resources
    fs_id = res["file_system_id"]
    have = {m.get("subnetId") for m in _mount_targets(client, fs_id)
            if m.get("status") not in ("deleting", "deleted")}
    made = 0
    for subnet in res["subnets"]:
        if subnet["subnet_id"] in have:
            continue
        client.create_mount_target(fileSystemId=fs_id, subnetId=subnet["subnet_id"],
                                   securityGroups=[res["mount_target_security_group"]])
        made += 1
        run.log("mount-targets", f"creating a mount target in {subnet['subnet_id']} "
                                 f"({subnet['availability_zone_id']})")
    wanted = {s["subnet_id"] for s in res["subnets"]}

    def available() -> bool:
        targets = [m for m in _mount_targets(client, fs_id) if m.get("subnetId") in wanted]
        if any(m.get("status") == "error" for m in targets):
            raise ProvisionError("a mount target failed to create: " + "; ".join(
                str(m.get("statusMessage") or m.get("mountTargetId")) for m in targets
                if m.get("status") == "error"))
        return len(targets) == len(wanted) and all(m.get("status") == "available"
                                                   for m in targets)

    _wait("the mount targets", available)
    run.record(mount_target_ids=[m["mountTargetId"] for m in _mount_targets(client, fs_id)
                                 if m.get("subnetId") in wanted])
    return f"{len(wanted)} mount target(s) available ({made} created)"


def _stage_access_point(run: _Run) -> str:
    client = run.s3files()
    res, request = run.resources, run.request
    fs_id = res["file_system_id"]
    name = access_point_name(run.storage_id)
    found = [a for a in _pages(client.list_access_points, "accessPoints", fileSystemId=fs_id)
             if a.get("name") == name and a.get("status") not in ("deleting", "deleted")]
    if found:
        ap = found[0]
        detail = f"adopted {ap['accessPointId']}"
    else:
        uid, gid = int(request.get("posix_uid", 1000)), int(request.get("posix_gid", 1000))
        # a directory of its own, created on first mount and owned by the access
        # point's identity: the file system root belongs to root, and an access point
        # rooted there cannot write as any other user
        root = request.get("root_directory") or "/data"
        ap = client.create_access_point(
            fileSystemId=fs_id, clientToken=_client_token(run.job_id, "ap"),
            posixUser={"uid": uid, "gid": gid},
            rootDirectory={"path": root, "creationPermissions": {
                "ownerUid": uid, "ownerGid": gid, "permissions": "755"}},
            tags=_s3files_tags({"Name": name, **run.tags}))
        detail = f"created {ap['accessPointId']} ({uid}:{gid}, {root})"
    run.record(access_point_id=ap["accessPointId"], access_point_arn=ap["accessPointArn"])
    ap_id = ap["accessPointId"]

    def available() -> bool:
        status = str(client.get_access_point(accessPointId=ap_id).get("status") or "")
        if status == "error":
            raise ProvisionError(f"access point {ap_id} failed to create")
        return status == "available"

    _wait(f"access point {ap_id}", available)
    return detail


def _stage_register(run: _Run) -> str:
    from app.services import storage_mounts

    request, res = run.request, run.resources
    if storage_mounts._row(run.db, run.workspace.id, run.storage_id) is None:
        storage_mounts.register(run.db, run.workspace.id, {
            "id": run.storage_id, "name": request.get("name") or run.storage_id,
            "access_point_arn": res["access_point_arn"],
            "max_mode": request.get("max_mode") or "ro",
            "data_class": request.get("data_class") or "reference",
            "network_profile_id": request.get("network_profile_id"),
        }, str(run.payload.get("actor") or "console"), origin="platform")
    for project_id in request.get("project_ids") or []:
        current = storage_mounts.allowed_bindings(run.db, run.workspace.id, project_id)
        if run.storage_id not in current:
            storage_mounts.set_project_entries(run.db, run.workspace.id, project_id,
                                               [*current, run.storage_id])
    try:
        checked = storage_mounts.run_check(run.db, run.workspace, run.storage_id)
    except AppError as exc:  # registered all the same; the page offers the check again
        return f"registered; the check could not run: {exc.message}"
    return f"registered, check {checked.get('status')}"


# ── delete stages ────────────────────────────────────────────────────────────


def _ap_id(res: dict[str, Any]) -> str | None:
    if res.get("access_point_id"):
        return str(res["access_point_id"])
    arn = str(res.get("access_point_arn") or "")
    return arn.rsplit("/", 1)[-1] if "/access-point/" in arn else None


def _fs_id(res: dict[str, Any]) -> str | None:
    if res.get("file_system_id"):
        return str(res["file_system_id"])
    arn = str(res.get("file_system_arn") or res.get("access_point_arn") or "")
    part = arn.split("file-system/", 1)[-1] if "file-system/" in arn else ""
    return part.split("/", 1)[0] or None


def _stage_validate_delete(run: _Run) -> str:
    from app.services import storage_mounts

    row = storage_mounts._row(run.db, run.workspace.id, run.storage_id)
    if row is None:
        return "already archived"
    storage_mounts.assert_unused(run.db, run.workspace.id, row)
    if not run.resources.get("mount_target_security_group"):
        # an entry whose create job is gone: find the group by its name and tag
        ec2 = run.ec2()
        groups = ec2.describe_security_groups(Filters=[
            {"Name": "group-name", "Values": [security_group_name(run.storage_id)]},
            {"Name": f"tag:{STORAGE_TAG}", "Values": [run.storage_id]},
        ]).get("SecurityGroups") or []
        if groups:
            run.record(mount_target_security_group=groups[0]["GroupId"])
    return f"no agent mounts '{row.name}'"


def _stage_delete_access_point(run: _Run) -> str:
    client = run.s3files()
    ap_id = _ap_id(run.resources)
    if not ap_id:
        return "no access point"
    try:
        client.delete_access_point(accessPointId=ap_id)
    except ClientError as exc:
        if not _gone(exc):
            raise
        return f"{ap_id} already gone"

    def gone() -> bool:
        try:
            return client.get_access_point(accessPointId=ap_id).get("status") == "deleted"
        except ClientError as exc:
            if _gone(exc):
                return True
            raise

    _wait(f"access point {ap_id}", gone)
    return f"deleted {ap_id}"


def _stage_delete_mount_targets(run: _Run) -> str:
    client = run.s3files()
    fs_id = _fs_id(run.resources)
    if not fs_id:
        return "no file system"
    try:
        targets = _mount_targets(client, fs_id)
    except ClientError as exc:
        if _gone(exc):
            return "file system already gone"
        raise
    for target in targets:
        if target.get("status") in ("deleting", "deleted"):
            continue
        try:
            client.delete_mount_target(mountTargetId=target["mountTargetId"])
        except ClientError as exc:
            if not _gone(exc):
                raise

    def gone() -> bool:
        try:
            return all(m.get("status") == "deleted" for m in _mount_targets(client, fs_id))
        except ClientError as exc:
            if _gone(exc):
                return True
            raise

    _wait("the mount targets", gone)
    return f"deleted {len(targets)} mount target(s)"


def _stage_delete_file_system(run: _Run) -> str:
    client = run.s3files()
    fs_id = _fs_id(run.resources)
    if not fs_id:
        return "no file system"
    try:
        client.delete_file_system(fileSystemId=fs_id)
    except ClientError as exc:
        if not _gone(exc):
            raise
        return f"{fs_id} already gone"

    def gone() -> bool:
        try:
            return _file_system_status(client, fs_id) == "deleted"
        except ClientError as exc:
            if _gone(exc):
                return True
            raise

    _wait(f"file system {fs_id}", gone)
    bucket = run.resources.get("bucket")
    return f"deleted {fs_id}" + (f"; bucket {bucket} kept" if bucket else "")


def _stage_delete_security_group(run: _Run) -> str:
    group_id = run.resources.get("mount_target_security_group")
    if not group_id:
        return "no security group"
    ec2 = run.ec2()

    def deleted() -> bool:
        try:
            ec2.delete_security_group(GroupId=group_id)
            return True
        except ClientError as exc:
            if _gone(exc):
                return True
            if _code(exc) == "DependencyViolation":  # mount target ENIs still releasing
                return False
            raise

    _wait(f"security group {group_id}", deleted)
    return f"deleted {group_id}"


def _stage_archive(run: _Run) -> str:
    from app.services import storage_mounts

    if storage_mounts._row(run.db, run.workspace.id, run.storage_id) is None:
        return "already archived"
    storage_mounts.archive(run.db, run.workspace.id, run.storage_id)
    bucket = run.resources.get("bucket")
    return "archived" + (f"; bucket {bucket} kept with its objects" if bucket else "")


STAGES: dict[str, dict[str, Callable[[_Run], str]]] = {
    OP_CREATE: {
        "validate": _stage_validate_create, "bucket": _stage_bucket,
        "file-system": _stage_file_system, "security-group": _stage_security_group,
        "mount-targets": _stage_mount_targets, "access-point": _stage_access_point,
        "register": _stage_register,
    },
    OP_DELETE: {
        "validate": _stage_validate_delete, "access-point": _stage_delete_access_point,
        "mount-targets": _stage_delete_mount_targets,
        "file-system": _stage_delete_file_system,
        "security-group": _stage_delete_security_group, "archive": _stage_archive,
    },
}


def _failure_text(exc: Exception) -> str:
    if isinstance(exc, ProvisionError | AppError):
        return str(getattr(exc, "message", None) or exc)
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        return f"{error.get('Code', 'ClientError')}: {error.get('Message', '')}"[:500]
    return f"{type(exc).__name__}: {exc}"[:500]


def execute(job_id: str, workspace_factory: Callable[[str | None], Any] | None = None) -> None:
    """Run (or resume) one job from its first unfinished stage; never raises."""
    from app.services.workspace import context_for_workspace

    db = SessionLocal()
    try:
        with project_context(None):
            job = db.get(Job, job_id)
            if job is None or job.type != JOB_TYPE or job.status not in ACTIVE:
                return
            resumed = job.status == "running"
            job.status, job.error = "running", None
            _log(job, "job", "resumed after a restart" if resumed else "started")
            db.commit()
            op = (job.payload or {}).get("op") or OP_CREATE
            workspace = (workspace_factory or context_for_workspace)(job.workspace_id)
            run = _Run(db, job_id, workspace)
            for name in _stage_names(op):
                done = next((s for s in run.payload.get("stages") or []
                             if s["name"] == name), {})
                if done.get("status") == "succeeded":
                    continue
                run.set_stage(name, "running")
                try:
                    detail = STAGES[op][name](run)
                except (ProvisionError, AppError, ClientError, BotoCoreError) as exc:
                    db.rollback()
                    text = _failure_text(exc)
                    run.set_stage(name, "failed", text)
                    row = run.job
                    row.status, row.error = "failed", f"{name}: {text}"
                    _log(row, name, text, "error")
                    db.commit()
                    return
                run.set_stage(name, "succeeded", detail)
                run.log(name, detail)
            row = run.job
            row.status = "succeeded"
            _log(row, "job", "succeeded")
            db.commit()
    except Exception as exc:  # never crash the worker thread; the job carries it
        db.rollback()
        logger.exception("%s %s failed", JOB_TYPE, job_id)
        with project_context(None):
            row = db.get(Job, job_id)
            if row is not None:
                row.status, row.error = "failed", f"{type(exc).__name__}: {exc}"[:500]
                db.commit()
    finally:
        db.close()


def start_async(job_id: str) -> threading.Thread:
    """One live worker per job in this process; also the resume starter."""
    with _LIVE_LOCK:
        existing = _LIVE.get(job_id)
        if existing is not None and existing.is_alive():
            return existing

        def work() -> None:
            try:
                execute(job_id)
            finally:
                with _LIVE_LOCK:
                    if _LIVE.get(job_id) is threading.current_thread():
                        del _LIVE[job_id]

        thread = threading.Thread(target=work, daemon=True, name=f"storage-prov-{job_id[:8]}")
        _LIVE[job_id] = thread
        try:
            thread.start()
        except Exception:
            _LIVE.pop(job_id, None)
            raise
        return thread
