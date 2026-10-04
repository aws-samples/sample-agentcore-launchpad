"""Container runtime wrappers — filesystemConfigurations + VPC param shapes."""

from app.deployer.container import _vpc
from app.deployer.filesystem import filesystem_configurations as _filesystem_configurations
from app.schemas.agent import AgentSpec
from app.services.agentcore import runtime as rt

S3_AP = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-abc/access-point/ap-1"
EFS_AP = "arn:aws:elasticfilesystem:us-west-2:111122223333:access-point/fsap-0123"


class StubControl:
    def __init__(self):
        self.created_with = None
        self.updated_with = None

    def create_agent_runtime(self, **kwargs):
        self.created_with = kwargs
        return {"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt-1", "status": "CREATING"}

    def update_agent_runtime(self, **kwargs):
        self.updated_with = kwargs
        return {"agentRuntimeId": "rt-1", "agentRuntimeVersion": "2", "status": "UPDATING"}


def _spec(**over) -> AgentSpec:
    return AgentSpec(name="fs-agent", method="container", system_prompt="hi", **over)


def test_spec_conversion_all_three_types():
    spec = _spec(
        filesystem={
            "session_storage": {"mount_path": "/mnt/workspace"},
            "s3_files": [{"access_point_arn": S3_AP, "mount_path": "/mnt/datasets"}],
            "efs": [{"access_point_arn": EFS_AP, "mount_path": "/mnt/tools"}],
        },
        network={"subnets": ["subnet-a", "subnet-b"], "security_groups": ["sg-1"]},
    )
    assert _filesystem_configurations(spec) == [
        {"sessionStorage": {"mountPath": "/mnt/workspace"}},
        {"s3FilesAccessPoint": {"accessPointArn": S3_AP, "mountPath": "/mnt/datasets"}},
        {"efsAccessPoint": {"accessPointArn": EFS_AP, "mountPath": "/mnt/tools"}},
    ]
    assert _vpc(spec) == {"subnets": ["subnet-a", "subnet-b"], "security_groups": ["sg-1"]}


def test_spec_conversion_session_disabled():
    spec = _spec(filesystem={"session_storage": None})
    assert _filesystem_configurations(spec) == []
    assert _vpc(spec) is None  # no BYO → PUBLIC even if network were set


def test_default_spec_yields_session_storage_public():
    spec = _spec()
    assert _filesystem_configurations(spec) == [
        {"sessionStorage": {"mountPath": "/mnt/workspace"}}
    ]
    assert _vpc(spec) is None


def test_create_runtime_public_default():
    stub = StubControl()
    rt.create_container_runtime(
        stub, runtime_name="a", container_uri="img", role_arn="arn:role"
    )
    assert stub.created_with["networkConfiguration"] == {"networkMode": "PUBLIC"}
    assert "filesystemConfigurations" not in stub.created_with


def test_create_runtime_with_filesystem_and_vpc():
    stub = StubControl()
    fs = [
        {"sessionStorage": {"mountPath": "/mnt/workspace"}},
        {"s3FilesAccessPoint": {"accessPointArn": S3_AP, "mountPath": "/mnt/datasets"}},
    ]
    rt.create_container_runtime(
        stub,
        runtime_name="a",
        container_uri="img",
        role_arn="arn:role",
        filesystem_configurations=fs,
        vpc={"subnets": ["subnet-a"], "security_groups": ["sg-1"]},
    )
    assert stub.created_with["filesystemConfigurations"] == fs
    assert stub.created_with["networkConfiguration"] == {
        "networkMode": "VPC",
        "networkModeConfig": {"subnets": ["subnet-a"], "securityGroups": ["sg-1"]},
    }


def test_update_runtime_with_filesystem_and_vpc():
    stub = StubControl()
    fs = [{"efsAccessPoint": {"accessPointArn": EFS_AP, "mountPath": "/mnt/tools"}}]
    rt.update_container_runtime(
        stub,
        runtime_id="rt-1",
        container_uri="img",
        role_arn="arn:role",
        filesystem_configurations=fs,
        vpc={"subnets": ["subnet-a"], "security_groups": ["sg-1"]},
    )
    assert stub.updated_with["filesystemConfigurations"] == fs
    assert stub.updated_with["networkConfiguration"]["networkMode"] == "VPC"


def test_update_runtime_public_no_fs():
    stub = StubControl()
    rt.update_container_runtime(
        stub, runtime_id="rt-1", container_uri="img", role_arn="arn:role"
    )
    assert stub.updated_with["networkConfiguration"] == {"networkMode": "PUBLIC"}
    assert "filesystemConfigurations" not in stub.updated_with


def test_update_runtime_can_clear_environment():
    stub = StubControl()
    rt.update_container_runtime(
        stub,
        runtime_id="rt-1",
        container_uri="img",
        role_arn="arn:role",
        environment={},
    )
    assert stub.updated_with["environmentVariables"] == {}
