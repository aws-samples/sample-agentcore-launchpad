"""The storage catalog (design storage-mounts §4, §9): the spec's catalog reference, the
admin register / check / edit / archive routes, the per-project allowed set, member
visibility (another project's entry reads as missing), the shared view, and the agent's
storage card. No AWS call is made: the storage and EC2 readers are fakes."""

import pytest
from pydantic import ValidationError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.models.ledger import Agent
from app.models.storage import StorageMount
from app.schemas.agent import AgentSpec
from app.services import storage_mounts as service
from tests.conftest import TEST_PROJECT, client_error
from tests.test_project_scope import (
    _login,
    _member_of,
    _project,
    _user,
    app,  # noqa: F401 — the fixture
)

VPC = "vpc-0123456789abcdef0"
S3_AP = ("arn:aws:s3files:us-west-2:111122223333:file-system/fs-0123456789abcdef0/"
         "access-point/fsap-0123456789abcdef0")
EFS_AP = "arn:aws:elasticfilesystem:us-west-2:111122223333:access-point/fsap-0aa"
MT_SG = "sg-0fffffffffffffff6"


def _facts(azs=("usw2-az1", "usw2-az2"), status="available"):
    return {
        "access_point": {"status": status, "file_system_id": "fs-0123456789abcdef0",
                         "posix_uid": 1000, "posix_gid": 1000, "root_directory": "/data"},
        "file_system": {"arn": S3_AP.split("/access-point/")[0], "status": "available",
                        "bucket_arn": "arn:aws:s3:::datasets"},
        "mount_targets": [
            {"id": f"mt-{i}", "subnet_id": f"subnet-mt{i}", "availability_zone_id": az,
             "vpc_id": VPC, "ip": f"10.0.{i}.10", "security_groups": [MT_SG],
             "status": "available"}
            for i, az in enumerate(azs)
        ],
    }


@pytest.fixture
def reader():
    calls = []
    facts = {"value": _facts()}

    def read(ctx, kind, arn):
        calls.append((kind, arn))
        value = facts["value"]
        if isinstance(value, Exception):
            raise value
        return value

    previous = service.set_reader(read)
    yield {"calls": calls, "facts": facts}
    service.set_reader(previous)


def _spec(**fields):
    return AgentSpec(name="fs-agent", method="container", system_prompt="p", **fields)


# ── the spec reference (§4.2) ─────────────────────────────────────────────────


def test_a_catalog_reference_defaults_to_read_only_under_its_id():
    spec = _spec(filesystem={"mounts": [{"storage_id": "datasets"}]},
                 network={"profile_id": "np-0123456789ab"})
    mount = spec.filesystem.mounts[0]
    assert (mount.mount_path, mount.mode, mount.resolved) == ("/mnt/datasets", "ro", False)
    assert spec.filesystem.byo and spec.filesystem.catalog_mounts() == []
    assert spec.filesystem.byo_mounts() == []  # the inline-only view is unchanged


@pytest.mark.parametrize("filesystem, match", [
    ({"mounts": [{"storage_id": "datasets", "kind": "s3files", "access_point_arn": S3_AP}]},
     "require VPC network"),
    ({"mounts": [{"storage_id": "Bad Id"}]}, "storage_id"),
    ({"mounts": [{"storage_id": "a1"}, {"storage_id": "a1", "mount_path": "/mnt/b"}]},
     "mounted once"),
    ({"mounts": [{"storage_id": "a1", "mount_path": "/mnt/x"},
                 {"storage_id": "a2", "mount_path": "/mnt/x"}]}, "unique"),
    ({"mounts": [{"storage_id": f"s{i}"} for i in range(4)],
      "efs": [{"access_point_arn": EFS_AP, "mount_path": "/mnt/e"}]}, "at most 4"),
    ({"mounts": [{"storage_id": "a1", "mode": "admin"}]}, "mode"),
    ({"mounts": [{"storage_id": "a1", "kind": "efs", "access_point_arn": S3_AP}]},
     "not an efs access point"),
])
def test_bad_references_are_refused(filesystem, match):
    network = None if match == "require VPC network" else {"profile_id": "np-0123456789ab"}
    with pytest.raises(ValidationError, match=match):
        _spec(filesystem=filesystem, network=network)


def test_kind_follows_the_access_point_arn():
    assert service.kind_of_arn(S3_AP) == "s3files"
    assert service.kind_of_arn(EFS_AP) == "efs"
    assert service.kind_of_arn("arn:aws:s3:::bucket") is None
    assert service.file_system_arn_of("s3files", S3_AP).endswith("fs-0123456789abcdef0")


# ── admin routes (§4.4, §9) ───────────────────────────────────────────────────


def test_register_checks_and_fills_the_entry(client, reader):
    res = client.post("/api/storage-mounts", json={"id": "datasets", "name": "Datasets",
                                                   "access_point_arn": S3_AP})
    assert res.status_code == 201, res.text
    entry = res.json()
    assert entry["kind"] == "s3files" and entry["max_mode"] == "ro"
    assert entry["status"] == "ok" and entry["checked_at"]
    assert entry["mount_target_azs"] == ["usw2-az1", "usw2-az2"]
    assert entry["vpc_id"] == VPC and entry["posix_uid"] == 1000
    assert entry["bucket_arn"] == "arn:aws:s3:::datasets"
    assert {r["check"] for r in entry["check_results"]} == {
        "access_point", "file_system", "mount_targets"}
    assert entry["agents"] == [] and entry["projects"] == []
    assert reader["calls"] == [("s3files", S3_AP)]

    dup = client.post("/api/storage-mounts", json={"id": "datasets", "access_point_arn": S3_AP})
    assert dup.status_code == 409 and dup.json()["code"] == "storage.id_exists"
    assert [e["id"] for e in client.get("/api/storage-mounts").json()["storage"]] == ["datasets"]


def test_a_failing_read_keeps_the_entry_unchecked(client, reader):
    reader["facts"]["value"] = client_error("AccessDeniedException")
    res = client.post("/api/storage-mounts", json={"id": "skills-lib", "access_point_arn": EFS_AP})
    assert res.status_code == 201
    assert res.json()["status"] == "unchecked"
    assert res.json()["check_error"]["code"] == "storage.check_failed"
    again = client.post("/api/storage-mounts/skills-lib/check")
    assert again.status_code == 502 and again.json()["detail"]["aws_code"] == (
        "AccessDeniedException")
    assert "infra/spoke" in again.json()["message"]


def test_a_missing_access_point_blocks_the_entry(client, reader):
    reader["facts"]["value"] = {"access_point": None, "file_system": None, "mount_targets": []}
    body = {"id": "gone", "access_point_arn": S3_AP}
    entry = client.post("/api/storage-mounts", json=body).json()
    assert entry["status"] == "blocked"
    assert entry["check_results"][0]["check"] == "access_point"


@pytest.mark.parametrize("bad", [
    {"id": "Data Sets"},
    {"access_point_arn": "arn:aws:s3:::not-an-access-point"},
    {"max_mode": "admin"},
    {"data_class": "secret"},
    {"extra": 1},
])
def test_bad_registrations_are_refused(client, reader, bad):
    res = client.post("/api/storage-mounts", json={"id": "datasets", "access_point_arn": S3_AP,
                                                   **bad})
    assert res.status_code == 422, res.text


def test_edit_archive_and_the_in_use_guards(client, reader):
    client.post("/api/storage-mounts", json={"id": "state", "access_point_arn": S3_AP,
                                             "max_mode": "rw", "data_class": "shared_state"})
    patched = client.patch("/api/storage-mounts/state", json={"name": "Pipeline state"}).json()
    assert patched["name"] == "Pipeline state" and patched["status"] == "ok"
    agent_id = _agent({"mounts": [{"storage_id": "state", "mode": "rw"}]}, TEST_PROJECT)
    shown = client.get("/api/storage-mounts/state").json()
    assert shown["agents"][0]["agent_id"] == agent_id and shown["writers"] == 1
    assert shown["multi_writer"] is False
    _agent({"mounts": [{"storage_id": "state", "mode": "rw"}]}, TEST_PROJECT, name="fs-two")
    assert client.get("/api/storage-mounts/state/usage").json()["multi_writer"] is True

    lowered = client.patch("/api/storage-mounts/state", json={"max_mode": "ro"})
    assert lowered.status_code == 409 and lowered.json()["code"] == "storage.mode_exceeds_max"
    busy = client.delete("/api/storage-mounts/state")
    assert busy.status_code == 409 and busy.json()["code"] == "storage.in_use"

    _delete_agents()
    assert client.delete("/api/storage-mounts/state").status_code == 204
    assert client.get("/api/storage-mounts/state").status_code == 404
    archived = client.get("/api/storage-mounts?include_archived=true").json()["storage"]
    assert archived[0]["archived_at"] is not None
    # the id is free again
    assert client.post("/api/storage-mounts", json={"id": "state", "access_point_arn": S3_AP,
                                                    "check": False}).status_code == 201


def test_the_project_allowed_set(client, reader):
    client.post("/api/storage-mounts", json={"id": "datasets", "access_point_arn": S3_AP})
    client.post("/api/storage-mounts", json={"id": "skills", "access_point_arn": EFS_AP})
    res = client.put(f"/api/storage-mounts/projects/{TEST_PROJECT}",
                     json={"storage_ids": ["datasets", "skills"]})
    assert res.status_code == 200, res.text
    assert [e["id"] for e in res.json()["storage"]] == ["datasets", "skills"]
    listed = {e["id"]: e for e in client.get("/api/storage-mounts").json()["storage"]}
    assert listed["datasets"]["allowed"] and listed["datasets"]["projects"] == [TEST_PROJECT]
    res = client.put(f"/api/storage-mounts/projects/{TEST_PROJECT}",
                     json={"storage_ids": ["skills"]})
    assert [e["id"] for e in res.json()["storage"]] == ["skills"]
    unknown = client.put(f"/api/storage-mounts/projects/{TEST_PROJECT}",
                         json={"storage_ids": ["nope"]})
    assert unknown.status_code == 404 and unknown.json()["code"] == "storage.not_found"
    nowhere = client.get("/api/storage-mounts/projects/not-bound-here")
    assert nowhere.status_code == 404 and nowhere.json()["code"] == "project.not_found"
    assert client.delete("/api/storage-mounts/skills").status_code == 204
    assert client.get(f"/api/storage-mounts/projects/{TEST_PROJECT}").json()["storage"] == []


def test_an_entry_of_another_workspace_reads_as_missing(client, reader):
    client.post("/api/storage-mounts", json={"id": "datasets", "access_point_arn": S3_AP})
    db = SessionLocal()
    try:
        db.query(StorageMount).one().workspace_id = "elsewhere"
        db.commit()
    finally:
        db.close()
    assert client.get("/api/storage-mounts/datasets").status_code == 404
    assert client.post("/api/storage-mounts/datasets/check").status_code == 404
    assert client.patch("/api/storage-mounts/datasets", json={"name": "x"}).status_code == 404
    assert client.get("/api/storage-mounts").json()["storage"] == []


# ── members (§5.4) ────────────────────────────────────────────────────────────


def _register(storage_id: str, arn: str = S3_AP, **fields) -> None:
    db = SessionLocal()
    try:
        service.register(db, DEFAULT_WORKSPACE_ID,
                         {"id": storage_id, "access_point_arn": arn, **fields}, "operator")
    finally:
        db.close()


def _allow(project_id: str, *storage_ids: str) -> None:
    db = SessionLocal()
    try:
        service.set_project_entries(db, DEFAULT_WORKSPACE_ID, project_id, list(storage_ids))
    finally:
        db.close()


def _agent(filesystem, project_id, *, name="fs-agent", network=None):
    db = SessionLocal()
    try:
        with project_context(project_id):
            row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="container",
                        status="active", version="3",
                        spec={"name": name, "method": "container", "filesystem": filesystem,
                              "network": network or {"profile_id": "np-0123456789ab"}})
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _delete_agents():
    db = SessionLocal()
    try:
        with project_context(None):
            for row in db.query(Agent).all():
                row.status = "deleted"
            db.commit()
    finally:
        db.close()


def test_a_member_sees_only_the_projects_allowed_storage(app):  # noqa: F811
    _project("alpha")
    _register("mine")
    _register("theirs", EFS_AP)
    _allow("alpha", "mine")
    _allow(TEST_PROJECT, "theirs")
    uid = _user("joan", grant=False)
    _member_of("alpha", uid)
    jo = _login(app, "joan")

    listed = jo.get("/api/storage-mounts").json()["storage"]
    assert [e["id"] for e in listed] == ["mine"]
    assert "projects" not in listed[0] and "agents" not in listed[0]
    assert jo.get("/api/storage-mounts/mine").status_code == 200
    hidden = jo.get("/api/storage-mounts/theirs")
    assert hidden.status_code == 404 and hidden.json()["code"] == "storage.not_found"
    assert jo.get("/api/storage-mounts/theirs/usage").status_code == 404
    assert jo.get("/api/storage-mounts/nope").json()["code"] == "storage.not_found"

    # another project's agents stay invisible in the usage view
    _agent({"mounts": [{"storage_id": "mine", "mode": "ro"}]}, TEST_PROJECT, name="other-agent")
    own = _agent({"mounts": [{"storage_id": "mine", "mode": "ro"}]}, "alpha", name="own-agent")
    used = jo.get("/api/storage-mounts/mine/usage").json()
    assert [a["agent_id"] for a in used["agents"]] == [own]

    # the agent card: own agent yes, another project's agent 404
    card = jo.get(f"/api/storage-mounts/agents/{own}")
    assert card.status_code == 200, card.text
    assert card.json()["mounts"][0]["entry"]["name"] == "mine"
    other = _agent({"mounts": []}, TEST_PROJECT, name="hidden-agent")
    assert jo.get(f"/api/storage-mounts/agents/{other}").status_code == 404

    # writes and the allowed set are administrators'
    body = {"id": "new", "access_point_arn": S3_AP}
    assert jo.post("/api/storage-mounts", json=body).status_code == 403
    assert jo.post("/api/storage-mounts/mine/check").status_code == 403
    assert jo.patch("/api/storage-mounts/mine", json={"name": "x"}).status_code == 403
    assert jo.delete("/api/storage-mounts/mine").status_code == 403
    assert jo.put("/api/storage-mounts/projects/alpha",
                  json={"storage_ids": ["theirs"]}).status_code == 403


def test_the_agent_card_lists_catalog_and_inline_mounts(client):
    _register("datasets")
    agent_id = _agent({"session_storage": {"mount_path": "/mnt/workspace"},
                       "s3_files": [{"access_point_arn": S3_AP, "mount_path": "/mnt/raw"}],
                       "mounts": [{"storage_id": "datasets", "mode": "ro"},
                                  {"storage_id": "gone", "mount_path": "/mnt/g"}]}, TEST_PROJECT)
    card = client.get(f"/api/storage-mounts/agents/{agent_id}").json()
    assert card["version"] == "3" and card["session_storage"] == "/mnt/workspace"
    assert card["mounts"][0] == {
        "storage_id": "datasets", "mount_path": "/mnt/datasets", "mode": "ro",
        "missing": False, "entry": card["mounts"][0]["entry"]}
    assert card["mounts"][0]["entry"]["status"] == "unchecked"
    assert card["mounts"][1]["missing"] is True
    assert card["inline"] == [{"kind": "s3files", "access_point_arn": S3_AP,
                               "mount_path": "/mnt/raw", "mode": "rw"}]
