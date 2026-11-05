"""The console's S3 Files creation and teardown (design storage-mounts §7,
`services/storage_provision`): every stage against fake S3 / S3 Files / EC2 clients, the
resume after a failed stage, the queue guards, the teardown that keeps the bucket, and
the routes. No AWS call is made."""

from types import SimpleNamespace

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.models.ledger import Job
from app.models.network import NetworkProfile
from app.services import storage_mounts, workspace_iam
from app.services import storage_provision as prov
from tests.conftest import TEST_PROJECT, client_error

ACCOUNT, REGION = "111122223333", "us-west-2"
VPC = "vpc-0123456789abcdef0"
AGENT_SG = "sg-0aaaaaaaaaaaaaaa1"
SYNC_ROLE = f"arn:aws:iam::{ACCOUNT}:role/launchpad-s3files-sync"
PROFILE = "np-0123456789ab"


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeS3:
    def __init__(self):
        self.buckets: dict[str, dict] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []

    def head_bucket(self, Bucket, ExpectedBucketOwner):  # noqa: N803
        if Bucket not in self.buckets:
            raise client_error("404", operation="HeadBucket")

    def create_bucket(self, Bucket, **kw):  # noqa: N803
        self.buckets[Bucket] = {"config": kw, "tags": []}
        self.created.append(Bucket)

    def put_public_access_block(self, Bucket, PublicAccessBlockConfiguration):  # noqa: N803
        self.buckets[Bucket]["public_access"] = PublicAccessBlockConfiguration

    def put_bucket_versioning(self, Bucket, VersioningConfiguration):  # noqa: N803
        self.buckets[Bucket]["versioning"] = VersioningConfiguration["Status"]

    def put_bucket_tagging(self, Bucket, Tagging):  # noqa: N803
        self.buckets[Bucket]["tags"] = Tagging["TagSet"]

    def get_bucket_tagging(self, Bucket):  # noqa: N803
        return {"TagSet": self.buckets[Bucket]["tags"]}

    def delete_bucket(self, Bucket):  # noqa: N803 — never expected
        self.deleted.append(Bucket)


class FakeS3Files:
    def __init__(self):
        self.fs: dict[str, dict] = {}
        self.mts: dict[str, dict] = {}
        self.aps: dict[str, dict] = {}
        self.calls: list[str] = []
        self.fail_mount_target_once = False

    def _arn(self, fs_id):
        return f"arn:aws:s3files:{REGION}:{ACCOUNT}:file-system/{fs_id}"

    def create_file_system(self, bucket, roleArn, clientToken, tags):  # noqa: N803
        self.calls.append("create_file_system")
        fs_id = f"fs-{len(self.fs) + 1:017d}"
        self.fs[fs_id] = {"fileSystemId": fs_id, "fileSystemArn": self._arn(fs_id),
                          "bucket": bucket, "roleArn": roleArn, "status": "creating",
                          "tags": tags}
        return dict(self.fs[fs_id])

    def get_file_system(self, fileSystemId):  # noqa: N803
        fs = self.fs.get(fileSystemId)
        if fs is None or fs["status"] == "gone":
            raise client_error("ResourceNotFoundException", operation="GetFileSystem")
        if fs["status"] == "creating":
            fs["status"] = "available"
        if fs["status"] == "deleting":
            fs["status"] = "gone"
            raise client_error("ResourceNotFoundException", operation="GetFileSystem")
        return dict(fs)

    def list_file_systems(self, bucket):
        return {"fileSystems": [dict(f) for f in self.fs.values()
                                if f["bucket"] == bucket and f["status"] != "gone"]}

    def delete_file_system(self, fileSystemId):  # noqa: N803
        self.calls.append("delete_file_system")
        assert not [m for m in self.mts.values() if m["fileSystemId"] == fileSystemId
                    and m["status"] != "deleted"], "mount targets first"
        self.fs[fileSystemId]["status"] = "deleting"

    def create_mount_target(self, fileSystemId, subnetId, securityGroups):  # noqa: N803
        self.calls.append(f"create_mount_target:{subnetId}")
        if self.fail_mount_target_once:
            self.fail_mount_target_once = False
            raise client_error("BadRequest", "subnet has no free IPs", "CreateMountTarget")
        mt_id = f"fsmt-{len(self.mts) + 1:017d}"
        self.mts[mt_id] = {"mountTargetId": mt_id, "fileSystemId": fileSystemId,
                           "subnetId": subnetId, "securityGroups": securityGroups,
                           "status": "available"}

    def list_mount_targets(self, fileSystemId):  # noqa: N803
        if fileSystemId not in self.fs or self.fs[fileSystemId]["status"] == "gone":
            raise client_error("ResourceNotFoundException", operation="ListMountTargets")
        return {"mountTargets": [dict(m) for m in self.mts.values()
                                 if m["fileSystemId"] == fileSystemId]}

    def delete_mount_target(self, mountTargetId):  # noqa: N803
        self.calls.append("delete_mount_target")
        self.mts[mountTargetId]["status"] = "deleted"

    def create_access_point(self, fileSystemId, clientToken, posixUser, tags,  # noqa: N803
                            rootDirectory=None):  # noqa: N803
        self.calls.append("create_access_point")
        ap_id = f"fsap-{len(self.aps) + 1:017d}"
        name = next(t["value"] for t in tags if t["key"] == "Name")
        self.aps[ap_id] = {"accessPointId": ap_id, "fileSystemId": fileSystemId, "name": name,
                           "accessPointArn": f"{self._arn(fileSystemId)}/access-point/{ap_id}",
                           "posixUser": posixUser, "rootDirectory": rootDirectory,
                           "status": "available"}
        return dict(self.aps[ap_id])

    def list_access_points(self, fileSystemId):  # noqa: N803
        return {"accessPoints": [dict(a) for a in self.aps.values()
                                 if a["fileSystemId"] == fileSystemId]}

    def get_access_point(self, accessPointId):  # noqa: N803
        ap = self.aps.get(accessPointId)
        if ap is None or ap["status"] == "deleted":
            raise client_error("AccessPointNotFound", operation="GetAccessPoint")
        return dict(ap)

    def delete_access_point(self, accessPointId):  # noqa: N803
        self.calls.append("delete_access_point")
        self.aps[accessPointId]["status"] = "deleted"


class FakeEc2:
    def __init__(self):
        self.groups: dict[str, dict] = {}
        self.ingress: list[dict] = []
        self.dependency_violations = 0

    def describe_subnets(self, SubnetIds):  # noqa: N803
        azs = {"subnet-a": "usw2-az1", "subnet-a2": "usw2-az1", "subnet-b": "usw2-az2"}
        return {"Subnets": [{"SubnetId": s, "VpcId": VPC, "AvailabilityZoneId": azs[s]}
                            for s in SubnetIds]}

    def describe_security_groups(self, Filters):  # noqa: N803
        want = {f["Name"]: f["Values"][0] for f in Filters}
        return {"SecurityGroups": [g for g in self.groups.values()
                                   if g["GroupName"] == want.get("group-name")
                                   or g["GroupId"] == want.get("group-id")]}

    def create_security_group(self, GroupName, VpcId, Description, TagSpecifications):  # noqa: N803
        group_id = f"sg-0mt{len(self.groups) + 1:014d}"
        # what AWS gives every new group: one allow-all outbound rule
        self.groups[group_id] = {"GroupId": group_id, "GroupName": GroupName, "VpcId": VpcId,
                                 "Tags": TagSpecifications[0]["Tags"],
                                 "IpPermissionsEgress": [{
                                     "IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                                     "Ipv6Ranges": [], "PrefixListIds": [],
                                     "UserIdGroupPairs": []}]}
        return {"GroupId": group_id}

    def revoke_security_group_egress(self, GroupId, IpPermissions):  # noqa: N803
        group = self.groups[GroupId]
        if not group["IpPermissionsEgress"]:
            raise client_error("InvalidPermission.NotFound")
        assert all(set(p) <= {"IpProtocol", "IpRanges", "FromPort", "ToPort"}
                   for p in IpPermissions), IpPermissions  # empty lists stripped
        group["IpPermissionsEgress"] = []
        self.revoked = [*getattr(self, "revoked", []), GroupId]

    def authorize_security_group_ingress(self, GroupId, IpPermissions):  # noqa: N803
        if any(i["GroupId"] == GroupId for i in self.ingress):
            raise client_error("InvalidPermission.Duplicate")
        self.ingress.append({"GroupId": GroupId, "IpPermissions": IpPermissions})

    def delete_security_group(self, GroupId):  # noqa: N803
        if self.dependency_violations:
            self.dependency_violations -= 1
            raise client_error("DependencyViolation", operation="DeleteSecurityGroup")
        self.groups.pop(GroupId)


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setattr(prov, "POLL_S", 0)
    fakes = SimpleNamespace(s3=FakeS3(), s3files=FakeS3Files(), ec2=FakeEc2())
    workspace = SimpleNamespace(
        id=DEFAULT_WORKSPACE_ID, account_id=ACCOUNT, region=REGION, partition="aws",
        resources={prov.SYNC_ROLE_KEY: SYNC_ROLE},
        client=lambda service, **_kw: getattr(fakes, service))
    fakes.workspace = workspace
    checks: list[str] = []
    monkeypatch.setattr(storage_mounts, "run_check",
                        lambda db, ctx, sid: checks.append(sid) or {"status": "ok"})
    fakes.checks = checks
    db = SessionLocal()
    db.add(NetworkProfile(id=PROFILE, workspace_id=DEFAULT_WORKSPACE_ID, name="core",
                          vpc_id=VPC, subnet_ids=["subnet-a", "subnet-a2", "subnet-b"],
                          security_group_ids=[AGENT_SG]))
    db.commit()
    db.close()
    yield fakes


def _request(**over):
    return {"id": "datasets", "name": "Datasets", "network_profile_id": PROFILE,
            "max_mode": "rw", "data_class": "shared_state", "posix_uid": 1000,
            "posix_gid": 1000, "root_directory": "/data", "project_ids": [TEST_PROJECT],
            **over}


def _queue(aws, **over):
    db = SessionLocal()
    try:
        job, started = prov.queue_create(db, aws.workspace, _request(**over), "operator")
        return job.id, started
    finally:
        db.close()


def _run(aws, job_id):
    prov.execute(job_id, workspace_factory=lambda _wid: aws.workspace)
    db = SessionLocal()
    try:
        return prov.get_job(db, DEFAULT_WORKSPACE_ID, job_id)
    finally:
        db.close()


def _entry(storage_id="datasets"):
    db = SessionLocal()
    try:
        return storage_mounts._row(db, DEFAULT_WORKSPACE_ID, storage_id)
    finally:
        db.close()


# ── names and the sync role ──────────────────────────────────────────────────


def test_the_bucket_name_is_deterministic_short_and_under_the_sync_role_prefix():
    name = prov.bucket_name(ACCOUNT, "ap-southeast-2", "a-long-workspace-id", "x" * 32)
    assert name == prov.bucket_name(ACCOUNT, "ap-southeast-2", "a-long-workspace-id", "x" * 32)
    assert len(name) <= 63 and name.startswith(workspace_iam.S3FILES_BUCKET_PREFIX)
    assert name != prov.bucket_name(ACCOUNT, "us-east-1", "a-long-workspace-id", "x" * 32)


def test_the_sync_role_reaches_only_the_platform_buckets():
    trust = workspace_iam.s3files_sync_trust_policy(ACCOUNT, REGION)["Statement"][0]
    assert trust["Principal"] == {"Service": "elasticfilesystem.amazonaws.com"}
    assert trust["Condition"]["StringEquals"] == {"aws:SourceAccount": ACCOUNT}
    policy = workspace_iam.s3files_sync_role_policy(ACCOUNT)["Statement"]
    s3 = [s for s in policy if any(a.startswith("s3:") for a in s["Action"])]
    assert {s["Resource"] for s in s3} == {"arn:aws:s3:::launchpad-s3files-*",
                                           "arn:aws:s3:::launchpad-s3files-*/*"}


# ── create ───────────────────────────────────────────────────────────────────


def test_a_create_builds_everything_and_registers_a_platform_entry(aws):
    job_id, started = _queue(aws)
    assert started
    out = _run(aws, job_id)
    assert out["status"] == "succeeded", out
    assert [s["status"] for s in out["stages"]] == ["succeeded"] * len(prov.CREATE_STAGES)

    bucket = prov.bucket_name(ACCOUNT, REGION, DEFAULT_WORKSPACE_ID, "datasets")
    made = aws.s3.buckets[bucket]
    assert made["versioning"] == "Enabled"
    assert made["config"] == {"CreateBucketConfiguration": {"LocationConstraint": REGION}}
    assert all(made["public_access"].values())
    assert {"Key": "launchpad:storage", "Value": "datasets"} in made["tags"]

    (fs,) = aws.s3files.fs.values()
    assert fs["bucket"] == f"arn:aws:s3:::{bucket}" and fs["roleArn"] == SYNC_ROLE
    # one mount target per AZ: subnet-a2 shares usw2-az1 with subnet-a
    assert sorted(m["subnetId"] for m in aws.s3files.mts.values()) == ["subnet-a", "subnet-b"]
    (group,) = aws.ec2.groups.values()
    assert group["GroupName"] == "launchpad-mt-datasets" and group["VpcId"] == VPC
    assert {"Key": "launchpad:managed", "Value": "true"} in group["Tags"]
    (rule,) = aws.ec2.ingress
    perm = rule["IpPermissions"][0]
    assert (perm["FromPort"], perm["ToPort"]) == (2049, 2049)
    assert [p["GroupId"] for p in perm["UserIdGroupPairs"]] == [AGENT_SG]
    # least privilege: the allow-all outbound rule a new group starts with is gone
    assert group["IpPermissionsEgress"] == []
    sg_stage = next(s for s in out["stages"] if s["name"] == "security-group")
    assert VPC in sg_stage["detail"] and "outbound none" in sg_stage["detail"]
    assert all(m["securityGroups"] == [group["GroupId"]] for m in aws.s3files.mts.values())
    (ap,) = aws.s3files.aps.values()
    assert ap["posixUser"] == {"uid": 1000, "gid": 1000}
    assert ap["rootDirectory"]["path"] == "/data"

    entry = _entry()
    assert entry.origin == "platform" and entry.access_point_arn == ap["accessPointArn"]
    assert (entry.max_mode, entry.data_class, entry.network_profile_id) == (
        "rw", "shared_state", PROFILE)
    assert aws.checks == ["datasets"]
    db = SessionLocal()
    try:
        assert "datasets" in storage_mounts.allowed_bindings(db, DEFAULT_WORKSPACE_ID,
                                                             TEST_PROJECT)
        with project_context(None):
            assert db.get(Job, job_id).project_platform_row()
    finally:
        db.close()


def test_a_failed_stage_resumes_without_making_anything_twice(aws):
    aws.s3files.fail_mount_target_once = True
    job_id, _ = _queue(aws)
    out = _run(aws, job_id)
    assert out["status"] == "failed" and out["error"].startswith("mount-targets:")
    assert "subnet has no free IPs" in out["error"]
    statuses = {s["name"]: s["status"] for s in out["stages"]}
    assert statuses["file-system"] == "succeeded" and statuses["mount-targets"] == "failed"

    again, started = _queue(aws)
    assert (again, started) == (job_id, True)  # the same job, resumed
    out = _run(aws, job_id)
    assert out["status"] == "succeeded", out
    assert len(aws.s3.created) == 1 and len(aws.ec2.groups) == 1
    assert aws.s3files.calls.count("create_file_system") == 1
    assert aws.s3files.calls.count("create_access_point") == 1


def test_a_second_request_while_one_runs_gets_that_job(aws):
    job_id, _ = _queue(aws)
    assert _queue(aws) == (job_id, False)


@pytest.mark.parametrize("change, code", [
    ({"resources": {}}, "storage.sync_role_missing"),
    ({"network_profile_id": "np-ffffffffffff"}, "network_profile.not_found"),
    ({"project_ids": ["no-such-project"]}, "project.not_found"),
])
def test_queue_guards(aws, change, code):
    if "resources" in change:
        aws.workspace.resources = change.pop("resources")
    with pytest.raises(AppError) as err:
        _queue(aws, **change)
    assert err.value.code == code


def test_an_existing_catalog_id_is_refused(aws):
    db = SessionLocal()
    storage_mounts.register(db, DEFAULT_WORKSPACE_ID, {
        "id": "datasets",
        "access_point_arn": "arn:aws:s3files:us-west-2:111122223333:file-system/fs-1/"
                            "access-point/fsap-1"}, "operator")
    db.close()
    with pytest.raises(AppError) as err:
        _queue(aws)
    assert err.value.code == "storage.id_exists"


def test_a_bucket_of_someone_else_is_never_adopted(aws):
    bucket = prov.bucket_name(ACCOUNT, REGION, DEFAULT_WORKSPACE_ID, "datasets")
    aws.s3.buckets[bucket] = {"config": {}, "tags": [{"Key": "owner", "Value": "finance"}]}
    out = _run(aws, _queue(aws)[0])
    assert out["status"] == "failed" and "was not made for this storage" in out["error"]
    assert not aws.s3files.fs


# ── teardown ─────────────────────────────────────────────────────────────────


def _delete(aws, confirm="datasets"):
    db = SessionLocal()
    try:
        job, _ = prov.queue_delete(db, aws.workspace, "datasets", confirm, "operator")
        return job.id
    finally:
        db.close()


def test_a_teardown_deletes_what_the_create_made_and_keeps_the_bucket(aws):
    assert _run(aws, _queue(aws)[0])["status"] == "succeeded"
    aws.ec2.dependency_violations = 2  # the mount target ENIs are still releasing
    out = _run(aws, _delete(aws))
    assert out["status"] == "succeeded", out
    assert aws.s3files.calls[-4:] == ["delete_access_point", "delete_mount_target",
                                      "delete_mount_target", "delete_file_system"]
    assert not aws.ec2.groups
    bucket = prov.bucket_name(ACCOUNT, REGION, DEFAULT_WORKSPACE_ID, "datasets")
    assert bucket in aws.s3.buckets and not aws.s3.deleted
    assert "kept" in out["stages"][-1]["detail"]
    assert _entry() is None  # archived


def test_teardown_guards(aws):
    assert _run(aws, _queue(aws)[0])["status"] == "succeeded"
    with pytest.raises(AppError) as err:
        _delete(aws, confirm="data")
    assert err.value.code == "storage.confirm_mismatch"

    db = SessionLocal()
    storage_mounts.register(db, DEFAULT_WORKSPACE_ID, {
        "id": "theirs",
        "access_point_arn": "arn:aws:s3files:us-west-2:111122223333:file-system/fs-9/"
                            "access-point/fsap-9"}, "operator")
    with pytest.raises(AppError) as err:
        prov.queue_delete(db, aws.workspace, "theirs", "theirs", "operator")
    db.close()
    assert err.value.code == "storage.not_platform"


# ── routes ───────────────────────────────────────────────────────────────────


def test_the_routes_queue_and_report(client, aws, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(prov, "start_async", started.append)
    original = prov.queue_create
    # the request's workspace is the fake one (its resources carry the sync role)
    monkeypatch.setattr(prov, "queue_create",
                        lambda db, _ctx, body, actor: original(db, aws.workspace, body, actor))
    res = client.post("/api/storage-mounts/provision", json=_request(project_ids=[]))
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["op"] == "create" and body["status"] == "queued" and started == [body["job_id"]]
    assert [s["name"] for s in body["stages"]] == prov.CREATE_STAGES
    listed = client.get("/api/storage-mounts/provision/jobs").json()["jobs"]
    assert [j["job_id"] for j in listed] == [body["job_id"]]
    one = client.get(f"/api/storage-mounts/provision/jobs/{body['job_id']}")
    assert one.status_code == 200
    assert client.get("/api/storage-mounts/provision/jobs/nope").status_code == 404
    for root in ("data/../x", "/"):
        # "/" belongs to root: an access point there cannot write (measured live)
        bad = client.post("/api/storage-mounts/provision", json=_request(root_directory=root))
        assert bad.status_code == 422, root
    gone = client.post("/api/storage-mounts/missing/teardown", json={"confirm": "missing"})
    assert gone.status_code == 404


# ── plan (what the dialog shows before a create) ─────────────────────────────

OTHER_SG = "sg-0bbbbbbbbbbbbbbb2"


def _facts(*, agent_egress, endpoint_tables=("rtb-a",)):
    subnets = [{"SubnetId": "subnet-a", "VpcId": VPC, "AvailabilityZoneId": "usw2-az1",
                "CidrBlock": "10.0.1.0/24"},
               {"SubnetId": "subnet-a2", "VpcId": VPC, "AvailabilityZoneId": "usw2-az1",
                "CidrBlock": "10.0.3.0/24"},
               {"SubnetId": "subnet-b", "VpcId": VPC, "AvailabilityZoneId": "usw2-az2",
                "CidrBlock": "10.0.2.0/24"}]
    endpoints = [] if endpoint_tables is None else [{
        "VpcEndpointId": "vpce-s3", "ServiceName": f"com.amazonaws.{REGION}.s3",
        "VpcEndpointType": "Gateway", "RouteTableIds": list(endpoint_tables)}]
    return {
        "vpc_id": VPC, "subnets": subnets,
        "security_groups": [{"GroupId": AGENT_SG, "GroupName": "agents",
                             "IpPermissionsEgress": agent_egress}],
        "vpc": {"VpcId": VPC, "CidrBlock": "10.0.0.0/16"},
        "route_tables": [{"RouteTableId": "rtb-a", "Associations": [{"Main": True}]}],
        "endpoints": endpoints, "nat_gateways": [{"NatGatewayId": "nat-1"}],
    }


def _plan(aws, monkeypatch, facts, **kw):
    from app.services import network_preflight

    monkeypatch.setattr(network_preflight, "gather", lambda ec2, **_kw: facts)
    db = SessionLocal()
    try:
        return prov.plan(db, aws.workspace, PROFILE, **kw)
    finally:
        db.close()


def test_the_plan_names_the_group_its_rules_and_the_mount_targets(aws, monkeypatch):
    nfs_to_vpc = [{"IpProtocol": "tcp", "FromPort": 2049, "ToPort": 2049,
                   "IpRanges": [{"CidrIp": "10.0.0.0/16"}]}]
    out = _plan(aws, monkeypatch, _facts(agent_egress=nfs_to_vpc), storage_id="datasets")
    group = out["mount_target_security_group"]
    assert group == {"name": "launchpad-mt-datasets", "vpc_id": VPC,
                     "inbound": [{"protocol": "tcp", "port": 2049,
                                  "source_group_id": AGENT_SG}],
                     "outbound": []}
    assert out["mount_targets"] == [{"subnet_id": "subnet-a", "availability_zone_id": "usw2-az1"},
                                    {"subnet_id": "subnet-b", "availability_zone_id": "usw2-az2"}]
    (agents,) = out["agent_security_groups"]
    assert agents["status"] == "ok" and agents["name"] == "agents"
    assert agents["required_outbound"]["destination"] == "launchpad-mt-datasets"
    assert out["s3_gateway_endpoint"]["status"] == "ok"
    assert out["vpc_cidrs"] == ["10.0.0.0/16"] and out["nat_gateways"] == ["nat-1"]
    assert not aws.ec2.groups  # read-only: nothing made


@pytest.mark.parametrize("egress, status", [
    ([{"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}], "ok"),
    # 443 only: the agents could not reach NFS
    ([{"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
       "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}], "missing"),
    # a CIDR that misses one mount-target subnet
    ([{"IpProtocol": "tcp", "FromPort": 2049, "ToPort": 2049,
       "IpRanges": [{"CidrIp": "10.0.1.0/24"}]}], "missing"),
])
def test_the_plan_says_whether_the_agents_may_reach_nfs(aws, monkeypatch, egress, status):
    out = _plan(aws, monkeypatch, _facts(agent_egress=egress))
    assert out["agent_security_groups"][0]["status"] == status
    assert out["mount_target_security_group"]["name"] == "launchpad-mt-<id>"


@pytest.mark.parametrize("tables, status", [(None, "missing"), (["rtb-other"], "unrouted")])
def test_the_plan_flags_a_missing_s3_gateway_endpoint(aws, monkeypatch, tables, status):
    out = _plan(aws, monkeypatch, _facts(agent_egress=[], endpoint_tables=tables))
    gateway = out["s3_gateway_endpoint"]
    assert gateway["status"] == status
    assert gateway["service_name"] == f"com.amazonaws.{REGION}.s3"
    assert gateway["docs"].startswith("https://docs.aws.amazon.com/")
    if status == "unrouted":
        assert gateway["unrouted_subnets"] == ["subnet-a", "subnet-a2", "subnet-b"]


def test_the_plan_route_is_read_only_and_checks_the_profile(client, aws, monkeypatch):
    monkeypatch.setattr(prov, "plan", lambda db, ctx, pid, sid: {"profile": pid, "id": sid})
    res = client.post("/api/storage-mounts/provision/plan",
                      json={"network_profile_id": PROFILE, "id": "datasets"})
    assert res.status_code == 200 and res.json() == {"profile": PROFILE, "id": "datasets"}
    bad = client.post("/api/storage-mounts/provision/plan",
                      json={"network_profile_id": "not a profile"})
    assert bad.status_code == 422
