"""The `s3bucket` storage catalog kind (AgentCore Runtime V2 `s3BucketMount`): register
validation, the spec reference and its stamp, the V2 / method gate, the mapping to
`filesystemConfigurations`, the prefix-scoped IAM (golden shapes from the live probes
P4 / P4b, 2026-11-23), the isolated-project boundary, the check and the preflight, the
agent-facing note, the live-view match and promotion. No AWS call is made: the bucket
reader and the EC2 gather are fakes."""

import copy
import re

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal, _migrate_storage_bucket_columns
from app.core.errors import AppError
from app.deployer import mounts
from app.deployer.filesystem import filesystem_configurations
from app.models.ledger import Workspace
from app.models.storage import StorageMount
from app.schemas.agent import AgentSpec, StorageMountRef
from app.services import agent_iam
from app.services import resource_mapping as rm
from app.services import storage_mounts as service
from app.services.agentcore.filesystem_kinds import mounted_prefix
from app.services.project_iam import BoundaryInputs, boundary_policy_document
from tests.conftest import TEST_PROJECT, client_error, ws_ctx
from tests.test_project_scope import _project
from tests.test_storage_catalog import _allow

SID = "${aws:AgentCoreRuntimeSourceSid}"
KMS = "arn:aws:kms:us-west-2:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"
AGENT = "a" * 32
V2 = {"platform_version": "V2", "billing_model": "consumption"}


def _register(storage_id: str = "reports", **fields) -> None:
    db = SessionLocal()
    try:
        service.register(db, DEFAULT_WORKSPACE_ID,
                         {"id": storage_id, "kind": "s3bucket", "bucket": "acme-data",
                          "prefix": "team", "scope": "per_session", **fields}, "operator")
    finally:
        db.close()


def _row(storage_id: str = "reports") -> StorageMount:
    db = SessionLocal()
    try:
        row = db.query(StorageMount).filter_by(id=storage_id).one()
        db.expunge(row)
        return row
    finally:
        db.close()


def _facts(**over):
    base = {"exists": True, "reachable": True, "region": "us-west-2", "encryption": "sse-s3",
            "kms_key_arn": None, "public_access_blocked": True, "versioning": "Enabled",
            "unreadable": []}
    return {**base, **over}


@pytest.fixture
def bucket():
    state = {"facts": _facts(), "calls": []}

    def read(ctx, name, owner):
        state["calls"].append((name, owner))
        value = state["facts"]
        if isinstance(value, Exception):
            raise value
        return value

    previous = service.set_bucket_reader(read)
    yield state
    service.set_bucket_reader(previous)


def _normalize(*mount_dicts, method="zip_runtime", runtime_config=V2, creating=False,
               network=None):
    spec = {"name": "fs-agent", "method": method, "system_prompt": "p",
            "filesystem": {"mounts": list(mount_dicts)}, "network": network,
            "runtime_config": runtime_config}
    db = SessionLocal()
    try:
        return service.normalize_spec_storage(db, DEFAULT_WORKSPACE_ID, TEST_PROJECT, spec,
                                              creating=creating)
    finally:
        db.close()


def _stamped_spec(*mount_dicts, method="zip_runtime", **kw) -> AgentSpec:
    stored = _normalize(*mount_dicts, method=method, **kw)
    return AgentSpec.model_validate(stored)


# ── the spec reference ────────────────────────────────────────────────────────


def test_a_bucket_reference_is_public_ok_and_nfs_still_needs_a_vpc():
    mount = {"storage_id": "reports", "kind": "s3bucket", "bucket": "acme-data",
             "scope": "shared", "mode": "rw"}
    spec = AgentSpec(name="fs-agent", method="zip_runtime", system_prompt="p",
                     filesystem={"mounts": [mount]})
    assert spec.network is None and not spec.filesystem.needs_vpc
    assert spec.filesystem.bucket_mounts()[0].resolved
    nfs = {"storage_id": "lake", "kind": "s3files",
           "access_point_arn": "arn:aws:s3files:us-west-2:111122223333:file-system/fs-1/"
                               "access-point/fsap-1"}
    with pytest.raises(ValidationError, match="require VPC network"):
        AgentSpec(name="fs-agent", method="zip_runtime", system_prompt="p",
                  filesystem={"mounts": [mount, nfs]})


def test_every_filesystem_entry_counts_against_the_limit_of_five():
    mounts_ = [{"storage_id": f"b{i}", "kind": "s3bucket", "bucket": "acme-data",
                "scope": "shared", "mount_path": f"/mnt/b{i}"} for i in range(5)]
    with pytest.raises(ValidationError, match="at most 4"):
        AgentSpec(name="fs-agent", method="zip_runtime", system_prompt="p",
                  filesystem={"mounts": mounts_})
    # session storage off leaves room for the fifth
    spec = AgentSpec(name="fs-agent", method="zip_runtime", system_prompt="p",
                     filesystem={"session_storage": None, "mounts": mounts_})
    assert len(spec.filesystem.mounts) == 5
    with pytest.raises(ValidationError):
        AgentSpec(name="fs-agent", method="zip_runtime", system_prompt="p",
                  filesystem={"session_storage": None, "mounts": [
                      *mounts_, {"storage_id": "b6", "mount_path": "/mnt/b6"}]})


def test_a_stamped_bucket_mount_is_refused_on_the_harness_by_the_schema():
    mount = {"storage_id": "reports", "kind": "s3bucket", "bucket": "acme-data",
             "scope": "shared"}
    with pytest.raises(ValidationError, match="not supported by the harness method"):
        AgentSpec(name="fs-agent", method="harness", system_prompt="p",
                  filesystem={"mounts": [mount]})


@pytest.mark.parametrize("fields", [
    {"bucket": "Bad_Bucket"},
    {"bucket": "acme-data", "bucket_owner": "123"},
    {"bucket": "acme-data", "bucket_region": "mars"},
])
def test_a_reference_with_a_malformed_bucket_field_is_refused(fields):
    with pytest.raises(ValidationError):
        StorageMountRef(storage_id="reports", kind="s3bucket", **fields)


# ── register (R1.2) ───────────────────────────────────────────────────────────


def test_register_normalizes_and_stores_no_access_point(client, bucket):
    res = client.post("/api/storage-mounts", json={
        "id": "reports", "kind": "s3bucket", "bucket": "acme-data", "prefix": "team/out",
        "scope": "per_session", "bucket_owner": "444455556666", "bucket_region": "us-east-1",
        "max_mode": "rw"})
    assert res.status_code == 201, res.text
    entry = res.json()
    assert entry["kind"] == "s3bucket" and entry["prefix"] == "team/out/"
    assert (entry["bucket"], entry["scope"], entry["bucket_owner"], entry["bucket_region"]) == (
        "acme-data", "per_session", "444455556666", "us-east-1")
    assert entry["access_point_arn"] == ""
    assert bucket["calls"] == [("acme-data", "444455556666")]
    row = _row()
    assert row.access_point_arn == "" and row.file_system_arn is None


def test_the_scope_defaults_to_per_session_and_a_missing_prefix_is_the_whole_bucket(
        client, bucket):
    entry = client.post("/api/storage-mounts", json={
        "id": "reports", "bucket": "acme-data", "check": False}).json()
    assert (entry["kind"], entry["scope"], entry["prefix"]) == ("s3bucket", "per_session", "")


@pytest.mark.parametrize("body, code", [
    ({"bucket": "ab"}, "validation"),                                  # too short
    ({"bucket": "acme..data"}, "storage.invalid_bucket"),
    ({"bucket": "acme--use1-az4--x-s3"}, "storage.invalid_bucket"),   # directory bucket
    ({"bucket": "launchpad-artifacts-111122223333-us-west-2"}, "storage.invalid_bucket"),
    ({"bucket": "acme-data", "prefix": "a/*"}, "storage.invalid_bucket"),   # IAM wildcard
    ({"bucket": "acme-data", "prefix": "a/{runtimeSessionId}/"}, "storage.invalid_bucket"),
    ({"bucket": "acme-data", "prefix": "/lead"}, "storage.invalid_bucket"),
    ({"bucket": "acme-data", "prefix": "a//b"}, "storage.invalid_bucket"),
    ({"bucket": "acme-data", "prefix": "x" * 901}, "storage.invalid_bucket"),
    ({"bucket": "acme-data", "bucket_owner": "12"}, "storage.invalid_bucket"),
    ({"bucket": "acme-data", "bucket_region": "mars"}, "storage.invalid_bucket"),
    ({"bucket": "acme-data", "scope": "forever"}, "validation"),
    ({"bucket": "acme-data", "access_point_arn": "arn:aws:s3files:r:1:file-system/f/"
                                                 "access-point/a"}, "validation"),
    ({"kind": "s3bucket"}, "validation"),                              # no bucket
    ({"access_point_arn": "arn:aws:s3files:us-west-2:111122223333:file-system/f/access-point/a",
      "scope": "shared"}, "validation"),                                # bucket field on an AP
])
def test_bad_bucket_registrations_are_refused(client, bucket, body, code):
    res = client.post("/api/storage-mounts", json={"id": "reports", "check": False, **body})
    assert res.status_code == 422, res.text
    if code != "validation":
        assert res.json()["code"] == code


def test_the_bucket_fields_are_immutable(client, bucket):
    client.post("/api/storage-mounts", json={"id": "reports", "bucket": "acme-data"})
    for field, value in (("bucket", "other"), ("prefix", "x/"), ("scope", "shared"),
                         ("bucket_owner", "444455556666"), ("bucket_region", "us-east-1")):
        res = client.patch("/api/storage-mounts/reports", json={field: value})
        assert res.status_code == 422, (field, res.text)
    assert client.patch("/api/storage-mounts/reports", json={"name": "Reports"}).status_code == 200


def test_the_detail_carries_the_iam_and_the_bucket_policy(client, bucket):
    client.post("/api/storage-mounts", json={
        "id": "reports", "bucket": "acme-data", "prefix": "team", "scope": "per_session",
        "max_mode": "rw"})
    entry = client.get("/api/storage-mounts/reports").json()
    assert entry["iam_preview"][0]["Condition"]["StringLike"]["s3:prefix"] == [
        f"team/<agent_id>/{SID}/*", f"team/<agent_id>/{SID}"]
    policy = entry["bucket_policy"]
    sids = {s["Sid"] for s in policy["Statement"]}
    assert sids == {"LaunchpadAgentsList", "LaunchpadAgentsObjects",
                    "ConfineObjectsToOwnSession", "ConfineListToOwnSession"}
    confine = next(s for s in policy["Statement"] if s["Sid"] == "ConfineObjectsToOwnSession")
    assert confine["NotResource"] == f"arn:aws:s3:::acme-data/team/*/{SID}/*"
    assert confine["Condition"]["Null"] == {"aws:AgentCoreRuntimeSourceSid": "false"}
    shared = client.post("/api/storage-mounts", json={
        "id": "lake", "bucket": "acme-lake", "scope": "shared", "check": False}).json()
    assert {s["Sid"] for s in client.get("/api/storage-mounts/lake").json()[
        "bucket_policy"]["Statement"]} == {"LaunchpadAgentsList", "LaunchpadAgentsObjects"}
    assert shared["kind"] == "s3bucket"


# ── check (R1.7) ──────────────────────────────────────────────────────────────


def _checks(client, **register):
    body = {"id": "reports", "bucket": "acme-data", **register}
    return {r["check"]: r for r in client.post("/api/storage-mounts", json=body).json()[
        "check_results"]}, client.get("/api/storage-mounts/reports").json()


def test_a_healthy_bucket_is_ok_and_records_its_encryption(client, bucket):
    checks, entry = _checks(client)
    assert entry["status"] == "ok" and entry["encryption"] == "sse-s3"
    assert checks["s3.bucket"]["severity"] == "ok" and checks["s3.public_access"][
        "severity"] == "ok"
    assert checks["s3.versioning"]["severity"] == "info"


def test_a_missing_bucket_blocks(client, bucket):
    bucket["facts"] = _facts(exists=False, reachable=False)
    checks, entry = _checks(client)
    assert entry["status"] == "blocked" and checks["s3.bucket_missing"]["severity"] == "block"


def test_an_unreachable_bucket_blocks_and_names_the_owner(client, bucket):
    bucket["facts"] = _facts(reachable=False)
    checks, entry = _checks(client, bucket_owner="444455556666")
    assert entry["status"] == "blocked"
    assert "444455556666" in checks["s3.bucket_access"]["message"]


def test_a_bucket_in_another_region_blocks_unless_it_is_named(client, bucket):
    bucket["facts"] = _facts(region="us-east-1")
    checks, entry = _checks(client)
    assert entry["status"] == "blocked" and "s3.region_mismatch" in checks
    client.delete("/api/storage-mounts/reports")
    checks, entry = _checks(client, bucket_region="us-east-1")
    assert entry["status"] == "ok" and checks["s3.region"]["severity"] == "ok"


def test_a_kms_bucket_is_noted_and_its_key_is_stored(client, bucket):
    bucket["facts"] = _facts(encryption="sse-kms", kms_key_arn=KMS)
    checks, entry = _checks(client, max_mode="rw")
    assert entry["kms_key_arn"] == KMS and entry["encryption"] == "sse-kms"
    assert "GenerateDataKey" in checks["s3.kms"]["message"]
    assert entry["status"] == "ok"


def test_block_public_access_off_warns(client, bucket):
    bucket["facts"] = _facts(public_access_blocked=False)
    checks, entry = _checks(client)
    assert entry["status"] == "warn" and checks["s3.public_access"]["severity"] == "warn"


def test_an_unreadable_facet_warns_it_does_not_block(client, bucket):
    bucket["facts"] = _facts(unreadable=["encryption"], encryption=None)
    checks, entry = _checks(client)
    assert entry["status"] == "warn" and "s3.unreadable.encryption" in checks


def test_a_failing_head_keeps_the_entry_unchecked(client, bucket):
    bucket["facts"] = client_error("AccessDeniedException")
    res = client.post("/api/storage-mounts", json={"id": "reports", "bucket": "acme-data"})
    assert res.status_code == 201
    assert res.json()["status"] == "unchecked"
    assert res.json()["check_error"]["code"] == "storage.check_failed"


def test_the_default_reader_reads_the_bucket_through_the_s3_client():
    calls = []

    class S3:
        def head_bucket(self, **kw):
            calls.append(("head", kw))
            return {"ResponseMetadata": {"HTTPHeaders": {"x-amz-bucket-region": "us-west-2"}}}

        def get_bucket_encryption(self, Bucket):  # noqa: N803
            return {"ServerSideEncryptionConfiguration": {"Rules": [{
                "ApplyServerSideEncryptionByDefault": {
                    "SSEAlgorithm": "aws:kms", "KMSMasterKeyID": "1234abcd-12ab"}}]}}

        def get_public_access_block(self, Bucket):  # noqa: N803
            raise client_error("NoSuchPublicAccessBlockConfiguration")

        def get_bucket_versioning(self, Bucket):  # noqa: N803
            raise client_error("AccessDenied")

    class Ctx:
        region, account_id, partition = "us-west-2", "111122223333", "aws"

        def client(self, name):
            assert name == "s3"
            return S3()

    facts = service._read_s3bucket(Ctx(), "acme-data", "444455556666")
    assert calls == [("head", {"Bucket": "acme-data", "ExpectedBucketOwner": "444455556666"})]
    assert facts["encryption"] == "sse-kms"
    assert facts["kms_key_arn"] == "arn:aws:kms:us-west-2:444455556666:key/1234abcd-12ab"
    assert facts["public_access_blocked"] is False and facts["unreadable"] == ["versioning"]


# ── stamping, the gate, the mapping (R1.3-R1.5) ───────────────────────────────


def test_normalize_stamps_a_bucket_entry_and_replaces_client_values(bucket):
    _register(max_mode="rw", bucket_owner="444455556666", bucket_region="us-east-1")
    _allow(TEST_PROJECT, "reports")
    stored = _normalize({"storage_id": "reports", "mode": "rw", "kind": "efs",
                         "access_point_arn": "arn:x", "bucket": "evil", "scope": "shared"})
    mount = stored["filesystem"]["mounts"][0]
    assert mount == {
        "storage_id": "reports", "mode": "rw", "mount_path": "/mnt/reports", "kind": "s3bucket",
        "access_point_arn": None, "bucket": "acme-data", "prefix": "team/",
        "scope": "per_session", "bucket_owner": "444455556666", "bucket_region": "us-east-1",
        "kms_key_arn": None}
    AgentSpec.model_validate(stored)  # a stored spec parses on PUBLIC


def test_an_nfs_stamp_carries_no_bucket_fields(bucket):
    arn = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-1/access-point/fsap-1"
    db = SessionLocal()
    try:
        service.register(db, DEFAULT_WORKSPACE_ID, {"id": "lake", "access_point_arn": arn}, "o")
    finally:
        db.close()
    _allow(TEST_PROJECT, "lake")
    stored = _normalize({"storage_id": "lake", **{k: None for k in (
        "bucket", "prefix", "scope", "bucket_owner", "bucket_region", "kms_key_arn")}},
        network={"profile_id": "np-0123456789ab"})
    assert set(stored["filesystem"]["mounts"][0]) == {
        "storage_id", "mode", "mount_path", "kind", "access_point_arn"}


@pytest.mark.parametrize("kwargs, code", [
    ({"method": "harness"}, "storage.method_unsupported"),
    ({"runtime_config": {**V2, "platform_version": "V1"}}, "storage.requires_v2"),
    ({"runtime_config": None}, "storage.requires_v2"),   # an existing agent: platform unknown
])
def test_the_platform_gate(bucket, kwargs, code):
    _register()
    _allow(TEST_PROJECT, "reports")
    with pytest.raises(AppError) as caught:
        _normalize({"storage_id": "reports"}, **kwargs)
    assert caught.value.code == code and caught.value.status_code == 422


def _stored_storage(spec: dict, previous_spec):
    """`routers.agents._stored_storage` as the redeploy route calls it."""
    from types import SimpleNamespace

    from app.routers.agents import _stored_storage as stored_storage

    request = SimpleNamespace(state=SimpleNamespace(project=SimpleNamespace(id=TEST_PROJECT)))
    ws = SimpleNamespace(id=DEFAULT_WORKSPACE_ID, context=ws_ctx())
    db = SessionLocal()
    try:
        return stored_storage(db, ws, request, AgentSpec.model_validate(spec),
                              previous_spec=previous_spec)
    finally:
        db.close()


@pytest.mark.parametrize("previous, code", [
    ({"runtime_config": V2}, None),                                  # keeps the stored V2
    ({"runtime_config": {**V2, "platform_version": "V1"}}, "storage.requires_v2"),
    ({}, "storage.requires_v2"),                                     # a legacy agent
])
def test_a_redeploy_without_runtime_config_is_judged_on_the_stored_platform(
        bucket, previous, code):
    _register()
    _allow(TEST_PROJECT, "reports")
    spec = {"name": "fs-agent", "method": "zip_runtime", "system_prompt": "p",
            "filesystem": {"mounts": [{"storage_id": "reports"}]}}
    if code is None:
        assert _stored_storage(spec, previous).filesystem.mounts[0].kind == "s3bucket"
        return
    with pytest.raises(AppError) as caught:
        _stored_storage(spec, previous)
    assert caught.value.code == code


def test_a_new_agent_without_runtime_config_defaults_to_v2(bucket):
    _register()
    _allow(TEST_PROJECT, "reports")
    for method in ("zip_runtime", "container", "byoc"):
        stored = _normalize({"storage_id": "reports"}, method=method, runtime_config=None,
                            creating=True)
        assert stored["filesystem"]["mounts"][0]["kind"] == "s3bucket"
    with pytest.raises(AppError) as caught:  # still not on the Harness
        _normalize({"storage_id": "reports"}, method="harness", runtime_config=None,
                   creating=True)
    assert caught.value.code == "storage.method_unsupported"


def test_filesystem_entries_for_each_scope_and_the_optional_fields(bucket):
    _register("shared-data", scope="shared", prefix="lake", max_mode="rw")
    _register("whole", scope="shared", prefix="", bucket="acme-all")
    _register("mine", scope="per_session", prefix="", bucket_owner="444455556666",
              bucket_region="eu-west-1", bucket="acme-eu")
    _allow(TEST_PROJECT, "shared-data", "whole", "mine")
    spec = _stamped_spec({"storage_id": "shared-data", "mode": "rw"}, {"storage_id": "whole"},
                         {"storage_id": "mine", "mode": "ro"})
    assert filesystem_configurations(spec, AGENT)[1:] == [
        {"s3BucketMount": {"bucket": "acme-data", "mountPath": "/mnt/shared-data",
                           "prefix": "lake/"}},
        {"s3BucketMount": {"bucket": "acme-all", "mountPath": "/mnt/whole"}},
        {"s3BucketMount": {"bucket": "acme-eu", "mountPath": "/mnt/mine",
                           "prefix": f"{AGENT}/{{runtimeSessionId}}/",
                           "s3BucketOwner": "444455556666", "s3BucketRegion": "eu-west-1"}},
    ]


def test_a_per_session_mount_cannot_be_rendered_without_its_agent(bucket):
    _register()
    _allow(TEST_PROJECT, "reports")
    spec = _stamped_spec({"storage_id": "reports"})
    with pytest.raises(ValueError, match="agent id"):
        filesystem_configurations(spec)
    assert mounted_prefix("team/", "per_session", AGENT) == f"team/{AGENT}/{{runtimeSessionId}}/"
    assert mounted_prefix("team/", "shared", None) == "team/"


def test_the_session_guard_sees_the_stored_per_session_mount(bucket):
    from app.services.invoke_session import has_per_session_mount

    _register()
    _register("lake", scope="shared")
    _allow(TEST_PROJECT, "reports", "lake")
    per = _normalize({"storage_id": "reports"})
    shared = _normalize({"storage_id": "lake"})
    agent = type("A", (), {"id": AGENT, "spec": {
        "name": "a", "method": "zip_runtime", "system_prompt": "p", **per}})()
    assert has_per_session_mount(agent)
    agent.spec = {"name": "a", "method": "zip_runtime", "system_prompt": "p", **shared}
    assert not has_per_session_mount(agent)


# ── IAM (R1.6): golden shapes ─────────────────────────────────────────────────

ARN = "arn:aws:s3:::acme-data"
RW = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:DeleteObject"]


def _statements(mode, scope, *, prefix="team", kms=None, bucket_name="acme-data"):
    db = SessionLocal()
    try:
        service.register(db, DEFAULT_WORKSPACE_ID, {
            "id": "reports", "bucket": bucket_name, "prefix": prefix, "scope": scope,
            "max_mode": "rw", "kind": "s3bucket"}, "o")
        row = db.query(StorageMount).filter_by(id="reports").one()
        row.kms_key_arn = kms
        db.commit()
    finally:
        db.close()
    _allow(TEST_PROJECT, "reports")
    spec = _stamped_spec({"storage_id": "reports", "mode": mode})
    return service.storage_iam_statements(spec, agent_id=AGENT)


@pytest.mark.parametrize("mode, actions", [("ro", ["s3:GetObject"]), ("rw", RW)])
def test_per_session_iam_golden(bucket, mode, actions):
    assert _statements(mode, "per_session") == [
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": ARN,
         "Condition": {"StringLike": {"s3:prefix": [
             f"team/{AGENT}/{SID}/*", f"team/{AGENT}/{SID}"]}}},
        {"Effect": "Allow", "Action": actions, "Resource": f"{ARN}/team/{AGENT}/{SID}/*"},
    ]


@pytest.mark.parametrize("mode, actions", [("ro", ["s3:GetObject"]), ("rw", RW)])
def test_shared_iam_golden(bucket, mode, actions):
    assert _statements(mode, "shared") == [
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": ARN,
         "Condition": {"StringLike": {"s3:prefix": ["team/*", "team"]}}},
        {"Effect": "Allow", "Action": actions, "Resource": f"{ARN}/team/*"},
    ]


def test_a_whole_bucket_shared_mount_has_no_prefix_condition(bucket):
    assert _statements("ro", "shared", prefix="") == [
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": ARN},
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"{ARN}/*"},
    ]


@pytest.mark.parametrize("mode, actions", [("ro", ["kms:Decrypt"]),
                                           ("rw", ["kms:Decrypt", "kms:GenerateDataKey"])])
def test_a_kms_bucket_adds_the_key_grant(bucket, mode, actions):
    statements = _statements(mode, "shared", kms=KMS)
    assert statements[-1] == {"Effect": "Allow", "Action": actions, "Resource": KMS}


def test_the_role_policy_document_carries_the_agent_scoped_statements(bucket):
    _register(max_mode="rw")
    _allow(TEST_PROJECT, "reports")
    spec = _stamped_spec({"storage_id": "reports", "mode": "rw"})
    doc = agent_iam.fs_policy_document(spec, AGENT)
    assert doc["Statement"][1]["Resource"] == f"{ARN}/team/{AGENT}/{SID}/*"
    with pytest.raises(ValueError):
        agent_iam.fs_policy_document(spec)  # a per-session prefix names its agent


def test_nfs_only_specs_keep_their_policy_without_an_agent_id():
    spec = AgentSpec(name="fs-agent", method="zip_runtime", system_prompt="p")
    assert agent_iam.fs_policy_document(spec) is None


def test_a_policy_that_would_outgrow_a_role_is_refused(bucket, monkeypatch):
    _register()
    _allow(TEST_PROJECT, "reports")
    monkeypatch.setattr(service, "MAX_INLINE_POLICY_CHARS", 100)
    with pytest.raises(AppError) as caught:
        _normalize({"storage_id": "reports"})
    assert caught.value.code == "storage.policy_too_large"


# ── the isolated project's boundary ──────────────────────────────────────────


def _boundary_inputs(project_id=TEST_PROJECT):
    db = SessionLocal()
    try:
        ws = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        return BoundaryInputs(project_id=project_id, account_id=ws.account_id,
                              region=ws.region)
    finally:
        db.close()


def test_the_boundary_has_nothing_without_an_allowed_entry(bucket):
    _register()
    inp = _boundary_inputs()
    assert service.boundary_statements(inp) == []  # not allowed to the project
    _project("other-proj")
    _allow(TEST_PROJECT, "reports")
    assert service.boundary_statements(_boundary_inputs("other-proj")) == []
    assert service.boundary_statements(inp)[0]["Sid"] == "S3Mount0"


def test_the_boundary_fits_a_managed_policy_with_five_entries(bucket):
    from app.services import project_iam

    for i in range(5):
        _register(f"b{i}", bucket=f"acme-data-{i}", prefix=f"team{i}", max_mode="rw")
    _allow(TEST_PROJECT, *[f"b{i}" for i in range(5)])
    doc = boundary_policy_document(_boundary_inputs())
    assert project_iam.policy_chars(doc) <= project_iam.MAX_POLICY_CHARS


# ── preflight (R1.8) ──────────────────────────────────────────────────────────


def _preflight(spec_dict):
    db = SessionLocal()
    try:
        return service.preflight_spec(db, ws_ctx(), spec_dict, TEST_PROJECT)
    finally:
        db.close()


def _spec_dict(*mount_dicts, method="zip_runtime", network=None, runtime_config=None):
    return {"method": method, "filesystem": {"mounts": list(mount_dicts)},
            "network": network, "runtime_config": runtime_config}


def _checked(client, bucket_state=None):
    client.post("/api/storage-mounts", json={"id": "reports", "bucket": "acme-data"})
    _allow(TEST_PROJECT, "reports")


def test_a_bucket_mount_on_public_passes_without_any_nfs_check(client, bucket):
    _checked(client)
    report = _preflight(_spec_dict({"storage_id": "reports", "mode": "ro"}))
    assert report["status"] == "ok", report
    assert {r["check"] for r in report["results"]} == {"storage.status"}


def test_v1_and_the_harness_block(client, bucket):
    _checked(client)
    v1 = _preflight(_spec_dict({"storage_id": "reports"},
                               runtime_config={"platform_version": "V1"}))
    assert v1["status"] == "blocked" and any(
        r["check"] == "storage.requires_v2" for r in v1["results"])
    harness = _preflight(_spec_dict({"storage_id": "reports"}, method="harness"))
    assert harness["status"] == "blocked" and any(
        r["check"] == "storage.method" for r in harness["results"])


def test_a_mode_above_the_entry_max_blocks(client, bucket):
    _checked(client)
    report = _preflight(_spec_dict({"storage_id": "reports", "mode": "rw"}))
    assert any(r["check"] == "storage.mode" and r["severity"] == "block"
               for r in report["results"])


class _Net:
    """An EC2 gather fake for a VPC agent."""

    def __init__(self, *, endpoint=True, nat=False, fail=False):
        self.endpoint, self.nat, self.fail = endpoint, nat, fail

    def __call__(self, ec2, *, vpc_id, subnet_ids, security_group_ids):
        if self.fail:
            raise client_error("UnauthorizedOperation")
        subnets = [{"SubnetId": s} for s in subnet_ids]
        tables = [{"RouteTableId": "rtb-1", "Associations": [{"Main": True}]}]
        endpoints = [{"ServiceName": "com.amazonaws.us-west-2.s3", "VpcEndpointType": "Gateway",
                      "VpcEndpointId": "vpce-1", "RouteTableIds": ["rtb-1"]}] if self.endpoint \
            else []
        return {"vpc_id": vpc_id, "subnets": subnets, "route_tables": tables,
                "endpoints": endpoints,
                "nat_gateways": [{"NatGatewayId": "nat-1"}] if self.nat else []}


@pytest.fixture
def vpc(monkeypatch):
    from app.models.network import NetworkProfile
    from app.services import network_preflight

    db = SessionLocal()
    try:
        db.add(NetworkProfile(id="np-0123456789ab", workspace_id=DEFAULT_WORKSPACE_ID,
                              name="np", vpc_id="vpc-0123456789abcdef0",
                              subnet_ids=["subnet-1"], security_group_ids=["sg-1"],
                              status="ok"))
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(network_preflight, "gather", _Net())
    monkeypatch.setattr(service, "_ec2_reader", lambda *_a: {"subnets": [], "groups": []})
    return monkeypatch


@pytest.mark.parametrize("net, severity", [
    (_Net(endpoint=True), "ok"), (_Net(endpoint=False, nat=True), "info"),
    (_Net(endpoint=False), "warn"), (_Net(fail=True), "warn")])
def test_a_vpc_agent_needs_a_route_to_s3(client, bucket, vpc, net, severity):
    from app.services import network_preflight

    vpc.setattr(network_preflight, "gather", net)
    _checked(client)
    report = _preflight(_spec_dict({"storage_id": "reports"},
                                   network={"profile_id": "np-0123456789ab"}))
    check = next(r for r in report["results"] if r["check"] == "storage.s3_endpoint")
    assert check["severity"] == severity
    assert report["status"] in ("ok", "warn")  # never blocks


def test_a_cross_region_bucket_in_a_vpc_warns(client, bucket, vpc):
    client.post("/api/storage-mounts", json={"id": "reports", "bucket": "acme-data",
                                             "bucket_region": "us-east-1"})
    bucket["facts"] = _facts(region="us-east-1")
    _allow(TEST_PROJECT, "reports")
    report = _preflight(_spec_dict({"storage_id": "reports"},
                                   network={"profile_id": "np-0123456789ab"}))
    assert any(r["check"] == "storage.s3_region" and r["severity"] == "warn"
               for r in report["results"])


def test_an_nfs_mount_on_public_still_blocks_next_to_a_bucket(client, bucket):
    arn = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-1/access-point/fsap-1"
    db = SessionLocal()
    try:
        service.register(db, DEFAULT_WORKSPACE_ID, {"id": "lake", "access_point_arn": arn}, "o")
    finally:
        db.close()
    _checked(client)
    _allow(TEST_PROJECT, "reports", "lake")
    report = _preflight(_spec_dict({"storage_id": "reports"}, {"storage_id": "lake"}))
    blocked = [r for r in report["results"] if r["severity"] == "block"]
    assert [r["storage_id"] for r in blocked] == ["lake"]


def test_the_wizard_preflight_route_takes_the_platform(client, bucket):
    _checked(client)
    res = client.post("/api/storage-mounts/preflight", json={
        "mounts": [{"storage_id": "reports"}], "method": "zip_runtime",
        "platform_version": "V1"})
    assert res.status_code == 200 and res.json()["status"] == "blocked"
    ok = client.post("/api/storage-mounts/preflight", json={
        "mounts": [{"storage_id": "reports"}], "method": "zip_runtime"})
    assert ok.json()["status"] == "ok"


# ── what the agent is told (R1.9) ─────────────────────────────────────────────


def test_the_manifest_and_the_prompt_note(bucket):
    _register("reports", max_mode="rw")
    _register("lake", scope="shared", prefix="lake", bucket="acme-lake")
    _allow(TEST_PROJECT, "reports", "lake")
    spec = _stamped_spec({"storage_id": "reports", "mode": "rw"}, {"storage_id": "lake"})
    manifest = mounts.mount_manifest(spec)
    assert manifest == [
        {"name": "reports", "path": "/mnt/reports", "kind": "s3bucket", "mode": "rw",
         "scope": "per_session"},
        {"name": "lake", "path": "/mnt/lake", "kind": "s3bucket", "mode": "ro",
         "scope": "shared"}]
    note = mounts.prompt_note(spec)
    assert "## Shared directories" in note
    reports = next(line for line in note.splitlines() if "/mnt/reports" in line)
    assert "this session only" in reports and "no append, rename or in-place edit" in reports
    assert "write a new file or replace it" in reports
    lake = next(line for line in note.splitlines() if "/mnt/lake" in line)
    assert "read-only" in lake and "this session only" not in lake and "append" not in lake


# ── the live view and the card ────────────────────────────────────────────────


def test_a_live_bucket_entry_is_matched_to_its_catalog_entry(client, bucket, monkeypatch):
    import app.routers.storage_mounts as storage_router
    from tests.test_live_mounts import StubRuntime, _agent

    _register("reports")
    _register("lake", scope="shared", prefix="lake", bucket="acme-lake")
    agent_id = _agent()
    live = [
        {"s3BucketMount": {"bucket": "acme-data", "mountPath": "/mnt/reports",
                           "prefix": f"team/{agent_id}/{{runtimeSessionId}}/"}},
        {"s3BucketMount": {"bucket": "acme-lake", "mountPath": "/mnt/lake", "prefix": "lake/"}},
        {"s3BucketMount": {"bucket": "acme-other", "mountPath": "/mnt/other"}},
    ]
    monkeypatch.setattr(storage_router, "control_client", lambda _ctx: StubRuntime(live))
    out = client.get(f"/api/storage-mounts/agents/{agent_id}").json()["live"]
    by_path = {e["mount_path"]: e for e in out["entries"]}
    assert by_path["/mnt/reports"]["storage_id"] == "reports"
    assert by_path["/mnt/lake"]["storage_id"] == "lake"
    assert by_path["/mnt/other"]["storage_id"] is None
    assert by_path["/mnt/other"]["bucket"] == "acme-other"
    # an access-point row never matches a bucket entry (it has no ARN to match)
    assert all(e["access_point_arn"] is None for e in out["entries"])


# ── promotion (R1.10) ─────────────────────────────────────────────────────────


@pytest.fixture
def prod():
    db = SessionLocal()
    try:
        db.add(Workspace(id="prod-ws", name="Prod", account_id="444455556677",
                         region="us-west-2", tier="prod", bootstrap_status="ready"))
        db.commit()
        yield db
    finally:
        db.close()


SOURCE = {"name": "a", "method": "zip_runtime", "system_prompt": "p",
          "filesystem": {"mounts": [{
              "storage_id": "reports", "mode": "rw", "mount_path": "/mnt/reports",
              "kind": "s3bucket", "bucket": "dev-bucket", "prefix": "dev/",
              "scope": "per_session"}]}}


def test_promotion_restamps_the_bucket_fields_from_the_target_entry(prod):
    service.register(prod, "prod-ws", {
        "id": "reports", "bucket": "prod-bucket", "prefix": "prod", "scope": "shared",
        "bucket_owner": "444455556677", "max_mode": "rw"}, "o")
    res = rm.resolve_spec(prod, copy.deepcopy(SOURCE), source_workspace_id=DEFAULT_WORKSPACE_ID,
                          target_workspace_id="prod-ws")
    assert res.complete, res.unmapped
    mount = res.spec["filesystem"]["mounts"][0]
    assert (mount["bucket"], mount["prefix"], mount["scope"], mount["bucket_owner"]) == (
        "prod-bucket", "prod/", "shared", "444455556677")
    assert SOURCE["filesystem"]["mounts"][0]["bucket"] == "dev-bucket"  # not mutated
    assert rm.assert_storage_mapped(prod, SOURCE, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                    target_workspace_id="prod-ws", project_id=None) == ["reports"]


def test_a_target_entry_of_another_kind_is_unmapped(prod):
    arn = "arn:aws:s3files:us-west-2:444455556677:file-system/fs-1/access-point/fsap-1"
    service.register(prod, "prod-ws", {"id": "reports", "access_point_arn": arn}, "o")
    res = rm.resolve_spec(prod, copy.deepcopy(SOURCE), source_workspace_id=DEFAULT_WORKSPACE_ID,
                          target_workspace_id="prod-ws")
    assert not res.complete
    assert res.unmapped[0]["code"] == "storage.unmapped" and "another kind" in res.unmapped[0][
        "reason"]
    assert not res.resolved
    with pytest.raises(AppError) as caught:
        rm.assert_storage_mapped(prod, SOURCE, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                 target_workspace_id="prod-ws", project_id=None)
    assert caught.value.code == "storage.unmapped"
    assert caught.value.detail["target_kind"] == "s3files"


# ── the ledger (R1.1) ─────────────────────────────────────────────────────────


def test_an_old_ledger_gains_the_bucket_columns():
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE storage_mounts (pk VARCHAR(32) PRIMARY KEY, "
                          "id VARCHAR(32), access_point_arn VARCHAR(2048) NOT NULL)"))
        conn.execute(text("INSERT INTO storage_mounts VALUES ('p1', 'old', 'arn:x')"))
    _migrate_storage_bucket_columns(engine)
    _migrate_storage_bucket_columns(engine)  # idempotent
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM storage_mounts")).mappings().one()
    assert {"bucket", "prefix", "scope", "bucket_owner", "bucket_region", "encryption",
            "kms_key_arn"} <= set(row)
    assert row["bucket"] is None and row["id"] == "old"


def test_the_error_codes_have_console_copy():
    import pathlib

    locales = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "locales"
    codes = ("storage.requires_v2", "storage.method_unsupported", "storage.invalid_bucket",
             "storage.policy_too_large")
    for locale in ("en", "zh-CN"):
        text_ = (locales / locale / "common.json").read_text(encoding="utf-8")
        for code in codes:
            assert re.search(rf'"{re.escape(code)}"\s*:', text_), (locale, code)
