"""Session output files (`filesystem.outputs`, task 11-23-s3mount-p2): the spec field and its
stamp, the V2 / method / bucket gates, the `s3BucketMount` mapping, the role and boundary
IAM, the bucket policy and lifecycle (probe P4b shape), the agent-facing note, the files
API (owner / admin / other member), the path rules, and the best-effort prefix delete.
No AWS call is made: S3 is a fake."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import app.services.workspace as workspace_module
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.deployer import mounts
from app.deployer.filesystem import filesystem_configurations
from app.main import create_app
from app.models.ledger import Agent, ChatSession
from app.schemas.agent import AgentSpec
from app.services import agent_files, agent_iam, storage_mounts
from app.services import resource_mapping as rm
from app.services.agentcore.filesystem_kinds import (
    outputs_agent_prefix,
    outputs_bucket_name,
    outputs_prefix,
)
from app.services.invoke_session import has_per_session_mount
from app.services.project_iam import BoundaryInputs, boundary_policy_document
from tests import test_visibility as tv
from tests.conftest import TEST_PROJECT, client_error, ws_ctx
from tests.test_visibility import _add, _member

BUCKET = "launchpad-agent-files-111122223333-us-west-2"
AGENT = "a" * 32
SESSION = "s" * 40
SID = "${aws:AgentCoreRuntimeSourceSid}"
V2 = {"platform_version": "V2", "billing_model": "consumption"}
RESOURCES = {"agent_files_bucket": BUCKET}
STAMPED = {"mount_path": "/mnt/outputs", "bucket": BUCKET, "project_key": TEST_PROJECT}


def _spec(outputs: Any = STAMPED, method: str = "zip_runtime", **fs: Any) -> AgentSpec:
    body: dict[str, Any] = {"name": "files-agent", "method": method, "system_prompt": "p",
                            "filesystem": {"outputs": outputs, **fs}}
    if method != "harness":
        body["runtime_config"] = V2
    return AgentSpec.model_validate(body)


def _normalize(outputs: Any = None, *, resources: Any = RESOURCES, project: Any = TEST_PROJECT,
               **spec: Any) -> dict:
    body = {"name": "files-agent", "method": "zip_runtime", "system_prompt": "p",
            "runtime_config": V2, "filesystem": {"outputs": outputs or {}}, **spec}
    return storage_mounts.normalize_outputs(body, resources, project, creating=False)


# ── the spec field ───────────────────────────────────────────────────────────


def test_outputs_default_off_and_mount_path_default():
    assert AgentSpec.model_validate({"name": "a-1", "method": "zip_runtime",
                                     "system_prompt": "p"}).filesystem.outputs is None
    assert _spec({}).filesystem.outputs.mount_path == "/mnt/outputs"
    assert not _spec({}).filesystem.outputs.resolved
    assert _spec().filesystem.outputs.resolved


def test_outputs_counts_against_the_five_entries_and_the_unique_paths():
    arn = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-1/access-point/fsap-{}"
    efs = [{"access_point_arn": arn.format(i), "mount_path": f"/mnt/m{i}"} for i in range(2)]
    with pytest.raises(ValidationError, match="session output folder"):
        # session storage + 2 + 2 + outputs = 6
        _spec({}, s3_files=efs, efs=[
            {"access_point_arn": "arn:aws:elasticfilesystem:us-west-2:111122223333:"
                                 f"access-point/fsap-{i}", "mount_path": f"/mnt/e{i}"}
            for i in range(2)])
    with pytest.raises(ValidationError, match="unique"):
        _spec({"mount_path": "/mnt/workspace"})  # session storage's default path


def test_outputs_refused_on_the_harness_and_bad_paths():
    with pytest.raises(ValidationError, match="session output folder is not supported"):
        _spec({}, method="harness")
    with pytest.raises(ValidationError):
        _spec({"mount_path": "/data/out"})
    with pytest.raises(ValidationError):
        _spec({"bucket": "not a bucket!"})


# ── the stamp and the gates ──────────────────────────────────────────────────


def test_normalize_stamps_the_bucket_and_project_and_replaces_client_values():
    stored = _normalize({"mount_path": "/mnt/out", "bucket": "someone-elses-bucket",
                         "project_key": "other"})
    assert stored["filesystem"]["outputs"] == {
        "mount_path": "/mnt/out", "bucket": BUCKET, "project_key": TEST_PROJECT}
    assert _normalize({})["filesystem"]["outputs"]["mount_path"] == "/mnt/outputs"
    assert _normalize({}, project=None)["filesystem"]["outputs"]["project_key"] == "shared"


def test_a_spec_without_outputs_comes_back_untouched():
    spec = {"name": "a-1", "method": "zip_runtime", "filesystem": {"outputs": None}}
    assert storage_mounts.normalize_outputs(spec, RESOURCES, None) is spec


@pytest.mark.parametrize("kwargs, code", [
    ({"resources": {}}, "storage.agent_files_unavailable"),
    ({"resources": None}, "storage.agent_files_unavailable"),
    ({"runtime_config": {**V2, "platform_version": "V1"}}, "storage.requires_v2"),
    ({"runtime_config": None}, "storage.requires_v2"),
    ({"method": "harness"}, "storage.method_unsupported"),
])
def test_normalize_gates(kwargs, code):
    with pytest.raises(AppError) as caught:
        _normalize({}, **kwargs)
    assert caught.value.code == code and caught.value.status_code == 422


def test_the_route_helper_stamps_with_the_agents_own_project():
    """`_stored_storage` (create and redeploy): a re-publish files the prefix under the
    agent's project, not whatever project the request names."""
    from app.routers.agents import _stored_storage

    request = SimpleNamespace(state=SimpleNamespace(project=SimpleNamespace(id="req-proj")))
    ws = SimpleNamespace(id=DEFAULT_WORKSPACE_ID, context=ws_ctx(RESOURCES))
    spec = AgentSpec.model_validate({
        "name": "files-agent", "method": "zip_runtime", "system_prompt": "p",
        "filesystem": {"outputs": {}}})
    db = SessionLocal()
    try:
        created = _stored_storage(db, ws, request, spec, creating=True)
        assert created.filesystem.outputs.project_key == "req-proj"
        agent = SimpleNamespace(project_id="agent-proj")
        again = _stored_storage(db, ws, request, spec, previous_spec={"runtime_config": V2},
                                agent=agent)
        assert again.filesystem.outputs.project_key == "agent-proj"
        assert again.filesystem.outputs.bucket == BUCKET
        with pytest.raises(AppError) as caught:  # a bucket-less workspace
            _stored_storage(db, SimpleNamespace(id=ws.id, context=ws_ctx()), request, spec,
                            creating=True)
        assert caught.value.code == "storage.agent_files_unavailable"
    finally:
        db.close()


# ── the mapping ──────────────────────────────────────────────────────────────


def test_the_mount_is_the_per_session_prefix_of_the_agents_folder():
    entries = filesystem_configurations(_spec(), AGENT)
    assert entries == [
        {"sessionStorage": {"mountPath": "/mnt/workspace"}},
        {"s3BucketMount": {
            "bucket": BUCKET, "mountPath": "/mnt/outputs",
            "prefix": f"projects/{TEST_PROJECT}/agents/{AGENT}/sessions/{{runtimeSessionId}}/"}},
    ]
    assert outputs_prefix("shared", AGENT).startswith(f"projects/shared/agents/{AGENT}/")
    assert outputs_prefix(TEST_PROJECT, AGENT, "sid") == outputs_agent_prefix(
        TEST_PROJECT, AGENT) + "sessions/sid/"


def test_the_mount_needs_its_agent_and_its_stamp():
    with pytest.raises(ValueError, match="agent id"):
        filesystem_configurations(_spec(), None)
    with pytest.raises(ValueError, match="not stamped"):
        filesystem_configurations(_spec({}), AGENT)
    assert filesystem_configurations(_spec(None), AGENT) == [
        {"sessionStorage": {"mountPath": "/mnt/workspace"}}]


def test_session_id_guard_sees_the_outputs_mount():
    agent = SimpleNamespace(id=AGENT, spec=_spec().model_dump(mode="json"))
    assert has_per_session_mount(agent) is True
    plain = SimpleNamespace(id=AGENT, spec=_spec(None).model_dump(mode="json"))
    assert has_per_session_mount(plain) is False


# ── IAM ──────────────────────────────────────────────────────────────────────


def test_role_policy_is_read_write_on_the_agents_own_sessions_only():
    document = agent_iam.fs_policy_document(_spec(), AGENT)
    base = f"projects/{TEST_PROJECT}/agents/{AGENT}/sessions/{SID}"
    assert document == {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": f"arn:aws:s3:::{BUCKET}",
         "Condition": {"StringLike": {"s3:prefix": [f"{base}/*", base]}}},
        {"Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload",
                    "s3:DeleteObject"],
         "Resource": f"arn:aws:s3:::{BUCKET}/{base}/*"},
    ]}
    with pytest.raises(ValueError, match="agent id"):
        agent_iam.fs_policy_document(_spec(), None)
    assert agent_iam.fs_policy_document(_spec(None), AGENT) is None


def test_boundary_allows_only_the_project_prefix_and_only_when_an_agent_uses_it():
    base = dict(project_id="alpha", account_id="111122223333", region="us-west-2")
    assert not any(s["Sid"].startswith("AgentFiles")
                   for s in boundary_policy_document(BoundaryInputs(**base))["Statement"])
    doc = boundary_policy_document(BoundaryInputs(**base, agent_files_bucket=BUCKET))
    by_sid = {s["Sid"]: s for s in doc["Statement"]}
    assert by_sid["AgentFilesObjects"]["Resource"] == [
        f"arn:aws:s3:::{BUCKET}/projects/alpha/*"]
    assert by_sid["AgentFilesList"]["Condition"] == {
        "StringLike": {"s3:prefix": ["projects/alpha/*"]}}
    assert set(by_sid["AgentFilesObjects"]["Action"]) == {
        "s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:DeleteObject"}


def test_boundary_inputs_name_the_bucket_only_for_a_project_with_an_outputs_agent():
    from app.services import project_iam

    needs = project_iam._Needs()
    needs.add_spec(_spec(None))
    assert needs.outputs is False
    needs.add_spec(_spec())
    assert needs.outputs is True


# ── the bucket ───────────────────────────────────────────────────────────────


def test_the_bucket_policy_is_the_probe_verified_shape():
    arn = f"arn:aws:s3:::{BUCKET}"
    in_session = {"Null": {"aws:AgentCoreRuntimeSourceSid": "false"}}
    sessions = f"projects/*/agents/*/sessions/{SID}"
    policy = agent_files.bucket_policy(BUCKET)
    assert policy["Statement"] == [
        {"Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*",
         "Action": "s3:*", "Resource": [arn, f"{arn}/*"],
         "Condition": {"Bool": {"aws:SecureTransport": "false"}}},
        {"Sid": "DenyOtherSessionsObjects", "Effect": "Deny", "Principal": "*",
         "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                    "s3:AbortMultipartUpload"],
         "NotResource": f"{arn}/{sessions}/*", "Condition": in_session},
        {"Sid": "DenyListOutsideOwnSession", "Effect": "Deny", "Principal": "*",
         "Action": "s3:ListBucket", "Resource": arn,
         "Condition": {**in_session, "StringNotLike": {
             "s3:prefix": [f"{sessions}/*", sessions]}}},
    ]
    assert agent_files.bucket_policy(BUCKET, "aws-cn")["Statement"][0]["Resource"][0] \
        .startswith("arn:aws-cn:s3:::")


def test_the_lifecycle_expires_in_30_days_and_aborts_stale_uploads():
    rule = agent_files.lifecycle_configuration()["Rules"][0]
    assert rule["Status"] == "Enabled" and rule["Filter"] == {"Prefix": ""}
    assert rule["Expiration"] == {"Days": 30}
    assert rule["AbortIncompleteMultipartUpload"] == {"DaysAfterInitiation": 1}
    assert outputs_bucket_name("111122223333", "us-west-2") == BUCKET


# ── the agent-facing note ────────────────────────────────────────────────────


def test_the_manifest_and_prompt_present_the_output_folder():
    spec = _spec()
    manifest = mounts.mount_manifest(spec)
    assert manifest == [{"name": "outputs", "path": "/mnt/outputs", "kind": "s3bucket",
                         "mode": "rw", "scope": "per_session", "purpose": "outputs"}]
    note = mounts.prompt_note(spec)
    assert "/mnt/outputs (session output folder, read-write; this session only)" in note
    assert "download them from the chat for 30 days" in note
    assert "no append, rename or in-place edit" in note
    assert json.loads(mounts.mounts_env(spec)[mounts.ENV_MOUNTS]) == manifest
    assert mounts.mount_manifest(_spec(None)) == []


def test_a_republish_owns_the_outputs_path():
    stored = _spec().model_dump(mode="json")
    assert "/mnt/outputs" in mounts.manifest_paths(stored)
    assert mounts.manifest_paths(_spec(None).model_dump(mode="json")) == set()


# ── promotion ────────────────────────────────────────────────────────────────


def test_restamp_outputs_takes_the_targets_bucket_and_project():
    spec = _spec().model_dump(mode="json")
    assert storage_mounts.restamp_outputs(spec, {"agent_files_bucket": "target-bucket"}, "p2")
    assert spec["filesystem"]["outputs"] == {
        "mount_path": "/mnt/outputs", "bucket": "target-bucket", "project_key": "p2"}
    assert storage_mounts.restamp_outputs(spec, {}, "p2") is False
    assert storage_mounts.restamp_outputs({"filesystem": {}}, {}, None) is True


def test_a_target_without_the_bucket_cannot_receive_the_agent():
    spec = _spec().model_dump(mode="json")
    db = SessionLocal()
    try:
        ok = rm.resolve_spec(db, spec, source_workspace_id=DEFAULT_WORKSPACE_ID,
                             target_workspace_id="ws-target",
                             target_resources={"agent_files_bucket": "tgt-bucket"})
        assert ok.complete and ok.spec["filesystem"]["outputs"]["bucket"] == "tgt-bucket"
        missing = rm.resolve_spec(db, spec, source_workspace_id=DEFAULT_WORKSPACE_ID,
                                  target_workspace_id="ws-target", target_resources={})
        assert not missing.complete
        assert missing.unmapped[0]["code"] == "storage.agent_files_unavailable"
    finally:
        db.close()


# ── the agent's storage card ─────────────────────────────────────────────────


def test_agent_storage_reports_the_output_folder():
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="card", method="zip_runtime",
                      spec=_spec().model_dump(mode="json"))
        assert storage_mounts.agent_storage(db, agent)["outputs"] == {
            "mount_path": "/mnt/outputs", "bucket": BUCKET}
        agent.spec = _spec(None).model_dump(mode="json")
        assert storage_mounts.agent_storage(db, agent)["outputs"] is None
    finally:
        db.close()


# ── the read side ────────────────────────────────────────────────────────────


class FakeS3:
    """list_objects_v2 / delete_objects / generate_presigned_url over a key → time map."""

    def __init__(self, objects: dict[str, tuple[int, datetime]] | None = None, *,
                 page: int = 1000, fail: bool = False):
        self.objects = dict(objects or {})
        self.page = page
        self.fail = fail
        self.deleted: list[str] = []
        self.listed: list[str] = []
        self.presigned: list[dict] = []

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None, **_kw):  # noqa: N803
        assert Bucket == BUCKET
        if self.fail:
            raise client_error("AccessDenied", operation="ListObjectsV2")
        self.listed.append(Prefix)
        # the token is the last key returned, as S3's is (a delete between pages must
        # not shift the next one)
        keys = sorted(k for k in self.objects
                      if k.startswith(Prefix) and k > (ContinuationToken or ""))
        chunk = keys[:self.page]
        more = len(keys) > self.page
        out: dict[str, Any] = {
            "Contents": [{"Key": k, "Size": self.objects[k][0],
                          "LastModified": self.objects[k][1]} for k in chunk],
            "IsTruncated": more}
        if more:
            out["NextContinuationToken"] = chunk[-1]
        return out

    def delete_objects(self, Bucket, Delete, **_kw):  # noqa: N803
        assert len(Delete["Objects"]) <= 1000
        for obj in Delete["Objects"]:
            self.deleted.append(obj["Key"])
            self.objects.pop(obj["Key"], None)
        return {}

    def generate_presigned_url(self, op, Params, ExpiresIn):  # noqa: N803
        self.presigned.append({"op": op, **Params, "ExpiresIn": ExpiresIn})
        return f"https://s3.example/{Params['Key']}?sig"


def _agent_row(**over: Any) -> SimpleNamespace:
    base = {"id": AGENT, "project_id": TEST_PROJECT, "spec": _spec().model_dump(mode="json")}
    return SimpleNamespace(**{**base, **over})


NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
PREFIX = f"projects/{TEST_PROJECT}/agents/{AGENT}/sessions/{SESSION}/"


def test_list_files_newest_first_skips_folders_and_other_sessions():
    s3 = FakeS3({
        PREFIX + "old.txt": (3, NOW - timedelta(hours=2)),
        PREFIX + "sub/new.md": (2048, NOW),
        PREFIX + "sub/": (0, NOW),                                  # a folder placeholder
        f"projects/{TEST_PROJECT}/agents/{AGENT}/sessions/{'t' * 40}/x": (1, NOW),
    })
    out = agent_files.list_files(_agent_row(), SESSION, ws_ctx(), s3=s3)
    assert [f["key_rel"] for f in out["files"]] == ["sub/new.md", "old.txt"]
    assert out["files"][0] == {"key_rel": "sub/new.md", "size": 2048,
                               "last_modified": NOW.isoformat()}
    assert out["enabled"] is True and out["truncated"] is False
    assert out["retention_days"] == 30 and out["mount_path"] == "/mnt/outputs"
    assert s3.listed == [PREFIX]


def test_list_files_caps_at_1000_and_says_so():
    s3 = FakeS3({f"{PREFIX}f{i:04d}": (1, NOW) for i in range(1003)}, page=400)
    out = agent_files.list_files(_agent_row(), SESSION, ws_ctx(), s3=s3)
    assert len(out["files"]) == 1000 and out["truncated"] is True


def test_list_files_for_an_agent_without_the_folder_reads_nothing():
    s3 = FakeS3()
    out = agent_files.list_files(_agent_row(spec=_spec(None).model_dump(mode="json")),
                                 SESSION, ws_ctx(), s3=s3)
    assert out["enabled"] is False and out["files"] == [] and s3.listed == []
    odd = agent_files.list_files(_agent_row(), "../x", ws_ctx(), s3=s3)  # not a session id
    assert odd["enabled"] is False and s3.listed == []


def test_presign_uses_the_servers_prefix_and_an_attachment_disposition():
    s3 = FakeS3()
    out = agent_files.presign(_agent_row(), SESSION, "sub/réport 1.md", ws_ctx(), s3=s3)
    assert out == {"url": f"https://s3.example/{PREFIX}sub/réport 1.md?sig",
                   "name": "réport 1.md", "expires_in": 300}
    assert s3.presigned == [{
        "op": "get_object", "Bucket": BUCKET, "Key": f"{PREFIX}sub/réport 1.md",
        "ResponseContentDisposition": "attachment; filename*=UTF-8''r%C3%A9port%201.md",
        "ExpiresIn": 300}]


def test_presign_asks_for_a_sigv4_regional_host_client(monkeypatch):
    seen: dict = {}

    def fake_client(self, service, *a, **k):
        seen.update(service=service, **k)
        return FakeS3()

    monkeypatch.setattr(workspace_module.WorkspaceContext, "client", fake_client)
    agent_files.presign(_agent_row(), SESSION, "a.txt", ws_ctx())
    assert seen["service"] == "s3"
    assert seen["config"].signature_version == "s3v4" and seen["cache_token"]
    assert seen["config"].s3 == {"addressing_style": "virtual"}


@pytest.mark.parametrize("path", ["", "/etc/passwd", "../other/x", "a/../../x", "a//b",
                                  "./a", "a/.", "a\\b", "a\nb", "x" * 1025])
def test_presign_refuses_a_path_that_leaves_the_session_folder(path):
    with pytest.raises(AppError) as caught:
        agent_files.presign(_agent_row(), SESSION, path, ws_ctx(), s3=FakeS3())
    assert caught.value.code == "chat.invalid_file_path" and caught.value.status_code == 422


def test_presign_for_an_agent_without_the_folder_is_404():
    with pytest.raises(Exception) as caught:
        agent_files.presign(_agent_row(spec=_spec(None).model_dump(mode="json")), SESSION,
                            "a.txt", ws_ctx(), s3=FakeS3())
    assert caught.value.code == "chat.files_unavailable"


# ── the files API ────────────────────────────────────────────────────────────

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3({PREFIX + "report.md": (5, NOW)})
    monkeypatch.setattr(workspace_module.WorkspaceContext, "client",
                        lambda self, service, *a, **k: fake)
    return fake


def _api_agent(spec: dict | None = None) -> str:
    return _add(Agent(
        name="files-agent", method="zip_runtime", status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/files-abcdefghij",
        resource_id="files-abcdefghij",
        spec=spec if spec is not None else {
            "name": "files-agent", "method": "zip_runtime", "system_prompt": "p",
            "filesystem": {"outputs": {**STAMPED, "project_key": "shared"}}},
    ))


def _session(agent_id: str, actor: str, session_id: str = SESSION) -> None:
    _add(ChatSession(agent_id=agent_id, session_id=session_id, actor_id=actor, turns=1))


def _urls(agent_id: str, session_id: str = SESSION) -> tuple[str, str]:
    base = f"/api/chat/{agent_id}/sessions/{session_id}/files"
    return base, f"{base}/download"


def test_the_sessions_user_and_admins_read_the_files_others_get_404(app, admin, ada, bob, s3):
    ada_id, ada_client = ada
    _, bob_client = bob
    _carol_id, carol = _member(app, "carol", role="admin")
    agent_id = _api_agent()
    _session(agent_id, "ada")
    listing, download = _urls(agent_id)
    prefix = f"projects/shared/agents/{agent_id}/sessions/{SESSION}/"
    s3.objects = {prefix + "report.md": (5, NOW)}

    mine = ada_client.get(listing)
    assert mine.status_code == 200, mine.text
    assert [f["key_rel"] for f in mine.json()["files"]] == ["report.md"]
    got = ada_client.get(download, params={"path": "report.md"})
    assert got.status_code == 200 and got.json()["url"].startswith("https://s3.example/")
    assert s3.presigned[-1]["Key"] == prefix + "report.md"

    for client in (admin, carol):  # administrators read any session's files
        assert client.get(listing).status_code == 200
    for method_url in ((listing, None), (download, {"path": "report.md"})):
        res = bob_client.get(method_url[0], params=method_url[1])
        assert res.status_code == 404 and res.json()["code"] == "chat.session_not_found"
    assert len(s3.presigned) == 1  # bob's attempt never reached S3


def test_a_path_outside_the_folder_is_refused_before_s3(app, admin, ada, s3):
    _, ada_client = ada
    agent_id = _api_agent()
    _session(agent_id, "ada")
    _, download = _urls(agent_id)
    res = ada_client.get(download, params={"path": "../other-session/report.md"})
    assert res.status_code == 422 and res.json()["code"] == "chat.invalid_file_path"
    assert s3.presigned == []


def test_unknown_sessions_other_agents_and_off_agents(app, admin, ada, s3):
    _, ada_client = ada
    agent_id, other = _api_agent(), _add(Agent(
        name="other-agent", method="zip_runtime", status="active", spec={"name": "other-agent"}))
    _session(agent_id, "ada")
    listing, _ = _urls(agent_id, "n" * 40)  # no such session
    assert ada_client.get(listing).json()["code"] == "chat.session_not_found"
    cross, _ = _urls(other)  # the session belongs to another agent
    assert ada_client.get(cross).json()["code"] == "chat.session_not_found"
    _session(other, "ada")
    off = ada_client.get(cross)
    assert off.status_code == 200
    assert off.json()["enabled"] is False and off.json()["files"] == []
    assert s3.listed == []


def test_auth_disabled_console_reads_the_folder_of_the_default_actor(s3):
    client = TestClient(create_app())
    agent_id = _api_agent()
    prefix = f"projects/shared/agents/{agent_id}/sessions/{SESSION}/"
    s3.objects = {prefix + "a.txt": (1, NOW)}
    _session(agent_id, "river")
    listing, _ = _urls(agent_id)
    assert client.get(listing).json()["files"][0]["key_rel"] == "a.txt"


# ── delete ───────────────────────────────────────────────────────────────────

REAL_DELETE = agent_files.delete_agent_files


def _real_delete(monkeypatch) -> None:
    """conftest no-ops the delete hook for every test; put the real one back."""
    monkeypatch.setattr(agent_files, "delete_agent_files", REAL_DELETE)


def test_delete_removes_only_the_agents_prefix(monkeypatch):
    _real_delete(monkeypatch)
    mine = outputs_agent_prefix(TEST_PROJECT, AGENT)
    s3 = FakeS3({
        mine + f"sessions/{SESSION}/a.txt": (1, NOW),
        mine + "sessions/other/b.txt": (1, NOW),
        outputs_agent_prefix(TEST_PROJECT, "b" * 32) + "sessions/x/c.txt": (1, NOW),
        outputs_agent_prefix(TEST_PROJECT, AGENT + "0") + "sessions/x/d.txt": (1, NOW),
        f"projects/{TEST_PROJECT}/agents/{AGENT}": (1, NOW),         # no trailing slash
        "agents/pkg/deployment_package.zip": (1, NOW),
    })
    workspace = ws_ctx(RESOURCES)
    assert agent_files.delete_agent_files(_agent_row(), workspace, s3_client=s3) == 2
    assert sorted(s3.deleted) == [mine + "sessions/other/b.txt",
                                  mine + f"sessions/{SESSION}/a.txt"]
    assert set(s3.listed) == {mine}
    assert len(s3.objects) == 4


def test_delete_covers_the_stamped_project_and_the_shared_fallback(monkeypatch):
    _real_delete(monkeypatch)
    s3 = FakeS3()
    agent = _agent_row(project_id=None)
    agent_files.delete_agent_files(agent, ws_ctx(RESOURCES), s3_client=s3)
    assert set(s3.listed) == {outputs_agent_prefix("shared", AGENT),
                              outputs_agent_prefix(TEST_PROJECT, AGENT)}


def test_delete_pages_through_and_batches(monkeypatch):
    _real_delete(monkeypatch)
    mine = outputs_agent_prefix(TEST_PROJECT, AGENT)
    s3 = FakeS3({f"{mine}sessions/s/{i}": (1, NOW) for i in range(2500)}, page=1000)
    assert agent_files.delete_agent_files(_agent_row(), ws_ctx(RESOURCES), s3_client=s3) == 2500
    assert not s3.objects


@pytest.mark.parametrize("resources, agent", [
    ({}, _agent_row()),                                      # a workspace without the bucket
    (RESOURCES, _agent_row(id="../..")),                     # an id that is not an agent id
    (RESOURCES, _agent_row(id="")),
])
def test_delete_skips_what_it_cannot_name_exactly(monkeypatch, resources, agent):
    _real_delete(monkeypatch)
    s3 = FakeS3({"projects/x/agents/y": (1, NOW)})
    assert agent_files.delete_agent_files(agent, ws_ctx(resources), s3_client=s3) == 0
    assert s3.listed == [] and s3.deleted == []


@pytest.mark.parametrize("key", ["a/b", "*", "..", "x y", ""])
def test_delete_never_forms_a_prefix_from_a_malformed_project_key(monkeypatch, key):
    _real_delete(monkeypatch)
    s3 = FakeS3()
    spec = _spec().model_dump(mode="json")
    spec["filesystem"]["outputs"]["project_key"] = key
    agent = _agent_row(project_id=key, spec=spec)
    agent_files.delete_agent_files(agent, ws_ctx(RESOURCES), s3_client=s3)
    # only the shared fallback (a None project) can remain; nothing broader than an agent folder
    assert all(p.startswith("projects/") and p.endswith(f"/agents/{AGENT}/") and "*" not in p
               and p.count("/") == 3 for p in s3.listed)


def test_delete_never_raises(monkeypatch):
    _real_delete(monkeypatch)
    assert agent_files.delete_agent_files(
        _agent_row(), ws_ctx(RESOURCES), s3_client=FakeS3(fail=True)) == 0

    class Broken:
        def client(self, *_a, **_k):
            raise RuntimeError("no credentials")

        resources = RESOURCES

    assert agent_files.delete_agent_files(_agent_row(), Broken()) == 0


def test_agent_delete_calls_the_hook(monkeypatch):
    """`DELETE /api/agents/{id}` (and the project purge that rides it) empties the folder."""
    import app.routers.agents as agents_router

    calls: list[Any] = []
    monkeypatch.setattr(agent_files, "delete_agent_files",
                        lambda agent, workspace, *a, **k: calls.append(agent.id) or 0)
    monkeypatch.setattr(agents_router, "delete_agent_cloud_resource", lambda *_a: True)
    monkeypatch.setattr(agents_router, "delete_agent_role", lambda *_a: True)
    monkeypatch.setattr(agents_router, "delete_agent_registry_record", lambda *_a: True)
    agent = SimpleNamespace(id=AGENT)
    assert agents_router._delete_agent_resources(agent, ws_ctx(RESOURCES)) is True
    assert calls == [AGENT]
    calls.clear()  # a discovered runtime is not ours: nothing deleted, nothing to clean
    monkeypatch.setattr(agents_router, "delete_agent_cloud_resource", lambda *_a: False)
    assert agents_router._delete_agent_resources(agent, ws_ctx(RESOURCES)) is False
    assert calls == []
