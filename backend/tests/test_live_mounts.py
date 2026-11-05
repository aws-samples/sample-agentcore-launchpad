"""Mounts set outside Launchpad: a Runtime re-publish keeps them (UpdateAgentRuntime
would detach them), and the agent's storage card reads them back from AWS. Stub
control clients; no AWS call is made."""

import pytest

import app.routers.storage_mounts as storage_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import filesystem
from app.models.ledger import Agent, SpecSnapshot
from app.services import storage_mounts
from tests.conftest import TEST_PROJECT

AP = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-1/access-point/fsap-{}"
EFS = "arn:aws:elasticfilesystem:us-west-2:111122223333:access-point/fsap-e1"
VPC = {"subnets": ["subnet-a"], "securityGroups": ["sg-a"]}


def _s3(n, path):
    return {"s3FilesAccessPoint": {"accessPointArn": AP.format(n), "mountPath": path}}


class StubRuntime:
    def __init__(self, live):
        self.live = live

    def get_agent_runtime(self, agentRuntimeId):  # noqa: N803
        return {"agentRuntimeId": agentRuntimeId, "filesystemConfigurations": self.live}


def test_merge_keeps_foreign_mounts_and_drops_the_platforms():
    platform = [{"sessionStorage": {"mountPath": "/mnt/workspace"}}, _s3(1, "/mnt/data")]
    live = [{"sessionStorage": {"mountPath": "/mnt/workspace"}},
            _s3(1, "/mnt/data"),               # the spec's own
            _s3(2, "/mnt/old"),                # an earlier publish set it, spec dropped it
            _s3(3, "/mnt/console"),            # added in the AWS console
            {"efsAccessPoint": {"accessPointArn": EFS, "mountPath": "/mnt/efs"}}]
    out = filesystem.merge_live(live, platform, managed_paths={"/mnt/old"})
    assert out == [*platform, _s3(3, "/mnt/console"), live[4]]
    assert filesystem.external_entries(live, platform, {"/mnt/old"}) == out[2:]


def test_a_conflict_at_a_spec_path_is_logged_and_the_spec_wins():
    logs: list[str] = []
    out = filesystem.merge_live([_s3(9, "/mnt/data")], [_s3(1, "/mnt/data")],
                                on_conflict=logs.append)
    assert out == [_s3(1, "/mnt/data")] and "/mnt/data" in logs[0]


@pytest.fixture
def spec(monkeypatch):
    # the spec's own entries, without building a VPC-bound AgentSpec
    monkeypatch.setattr(filesystem, "filesystem_configurations",
                        lambda _spec: [{"sessionStorage": {"mountPath": "/mnt/workspace"}}])
    return object()


def test_a_runtime_update_sends_the_console_mount_again(spec):
    logs: list[str] = []
    control = StubRuntime([{"sessionStorage": {"mountPath": "/mnt/workspace"}},
                           _s3(3, "/mnt/console")])
    out = filesystem.runtime_update_mounts(control, "rt-1", spec, managed_paths=set(),
                                           vpc=VPC, log=logs.append)
    assert out == [{"sessionStorage": {"mountPath": "/mnt/workspace"}}, _s3(3, "/mnt/console")]
    assert any("kept" in line for line in logs)


def test_a_runtime_moving_to_public_detaches_it_and_says_so(spec):
    logs: list[str] = []
    control = StubRuntime([_s3(3, "/mnt/console")])
    out = filesystem.runtime_update_mounts(control, "rt-1", spec, managed_paths=set(),
                                           vpc=None, log=logs.append)
    assert out == [{"sessionStorage": {"mountPath": "/mnt/workspace"}}]
    assert any("PUBLIC" in line for line in logs)


# ── the storage card reads the live mounts ───────────────────────────────────


def _agent(method="zip_runtime", spec=None) -> str:
    db = SessionLocal()
    row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, project_id=TEST_PROJECT, name="mnt-card",
                method=method, status="active", resource_id="rt-1", version="2",
                spec=spec or {"name": "mnt-card", "method": method, "system_prompt": "x",
                              "filesystem": {"s3_files": [
                                  {"access_point_arn": AP.format(1), "mount_path": "/mnt/data"}]}})
    db.add(row)
    db.commit()
    agent_id = row.id
    db.close()
    return agent_id


def test_the_card_marks_a_console_mount_external_and_names_its_catalog_entry(client,
                                                                             monkeypatch):
    agent_id = _agent()
    db = SessionLocal()
    storage_mounts.register(db, DEFAULT_WORKSPACE_ID,
                            {"id": "shared", "name": "Shared", "access_point_arn": AP.format(3)},
                            "operator")
    db.add(SpecSnapshot(agent_id=agent_id, seq=1, spec={"filesystem": {"efs": [
        {"access_point_arn": EFS, "mount_path": "/mnt/efs"}]}}))
    db.commit()
    db.close()
    control = StubRuntime([{"sessionStorage": {"mountPath": "/mnt/workspace"}},
                           _s3(1, "/mnt/data"), _s3(3, "/mnt/console"),
                           {"efsAccessPoint": {"accessPointArn": EFS, "mountPath": "/mnt/efs"}}])
    monkeypatch.setattr(storage_router, "control_client", lambda _ctx: control)
    live = client.get(f"/api/storage-mounts/agents/{agent_id}").json()["live"]
    assert live["status"] == "ok" and live["kind"] == "runtime"
    by_path = {e["mount_path"]: e for e in live["entries"]}
    assert set(by_path) == {"/mnt/data", "/mnt/console", "/mnt/efs"}
    assert by_path["/mnt/data"]["origin"] == "platform"
    assert by_path["/mnt/efs"]["origin"] == "platform" and by_path["/mnt/efs"]["kind"] == "efs"
    console = by_path["/mnt/console"]
    assert console["origin"] == "external"
    assert (console["storage_id"], console["storage_name"]) == ("shared", "Shared")


def test_an_aws_failure_only_blanks_the_live_part(client, monkeypatch):
    from tests.conftest import client_error

    agent_id = _agent()

    class Broken:
        def get_agent_runtime(self, **_kw):
            raise client_error("AccessDeniedException", operation="GetAgentRuntime")

    monkeypatch.setattr(storage_router, "control_client", lambda _ctx: Broken())
    card = client.get(f"/api/storage-mounts/agents/{agent_id}").json()
    assert card["live"] == {"status": "unavailable", "entries": [],
                            "error": "AccessDeniedException"}
    assert card["inline"]  # the ledger part is still there
