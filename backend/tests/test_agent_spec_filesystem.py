"""FilesystemConfig / VpcNetwork spec validation — pure pydantic, no AWS."""

import pytest
from pydantic import ValidationError

from app.schemas.agent import AgentSpec, FilesystemConfig

S3_AP = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-abc/access-point/ap-1"
EFS_AP = "arn:aws:elasticfilesystem:us-west-2:111122223333:access-point/fsap-0123"
VPC = {"subnets": ["subnet-a"], "security_groups": ["sg-a"]}

BASE = {"name": "fs-agent", "method": "zip_runtime", "system_prompt": "hi"}


def test_default_is_session_storage_on():
    spec = AgentSpec(**BASE)
    assert spec.filesystem.session_storage is not None
    assert spec.filesystem.session_storage.mount_path == "/mnt/workspace"
    assert spec.filesystem.s3_files == [] and spec.filesystem.efs == []
    assert spec.filesystem.byo is False
    assert spec.network is None


def test_old_stored_spec_revalidates():
    # pre-filesystem specs carry neither key — defaults must apply cleanly
    spec = AgentSpec(**{**BASE, "method": "harness", "skills": ["s3://b/skills/x/"]})
    assert spec.filesystem.session_storage.mount_path == "/mnt/workspace"


def test_explicit_null_disables_session_storage():
    spec = AgentSpec(**BASE, filesystem={"session_storage": None})
    assert spec.filesystem.session_storage is None


def test_custom_session_mount_path():
    spec = AgentSpec(**BASE, filesystem={"session_storage": {"mount_path": "/mnt/scratch"}})
    assert spec.filesystem.session_storage.mount_path == "/mnt/scratch"


@pytest.mark.parametrize(
    "bad_path",
    ["/data", "/mnt", "/mnt/", "/mnt/a/b", "mnt/data", "/mnt/sp ace", "/mnt/x*y"],
)
def test_mount_path_pattern_rejected(bad_path):
    with pytest.raises(ValidationError):
        FilesystemConfig(session_storage={"mount_path": bad_path})


def test_byo_requires_network():
    with pytest.raises(ValidationError, match="VPC network"):
        AgentSpec(
            **BASE,
            filesystem={"s3_files": [{"access_point_arn": S3_AP, "mount_path": "/mnt/data"}]},
        )


def test_byo_with_network_ok():
    spec = AgentSpec(
        **BASE,
        filesystem={
            "s3_files": [{"access_point_arn": S3_AP, "mount_path": "/mnt/data"}],
            "efs": [{"access_point_arn": EFS_AP, "mount_path": "/mnt/tools"}],
        },
        network=VPC,
    )
    assert spec.filesystem.byo is True
    assert spec.network.subnets == ["subnet-a"]


def test_session_only_must_not_require_network():
    assert AgentSpec(**BASE).network is None  # no ValidationError


def test_duplicate_mount_paths_rejected():
    with pytest.raises(ValidationError, match="unique"):
        FilesystemConfig(
            session_storage={"mount_path": "/mnt/data"},
            s3_files=[{"access_point_arn": S3_AP, "mount_path": "/mnt/data/"}],
        )


def test_more_than_two_s3_mounts_rejected():
    rows = [
        {"access_point_arn": S3_AP, "mount_path": f"/mnt/d{i}"} for i in range(3)
    ]
    with pytest.raises(ValidationError):
        FilesystemConfig(s3_files=rows)


def test_more_than_two_efs_mounts_rejected():
    rows = [
        {"access_point_arn": EFS_AP, "mount_path": f"/mnt/e{i}"} for i in range(3)
    ]
    with pytest.raises(ValidationError):
        FilesystemConfig(efs=rows)


def test_arn_service_sanity():
    with pytest.raises(ValidationError, match="S3 Files"):
        FilesystemConfig(s3_files=[{"access_point_arn": EFS_AP, "mount_path": "/mnt/d"}])
    with pytest.raises(ValidationError, match="EFS"):
        FilesystemConfig(efs=[{"access_point_arn": S3_AP, "mount_path": "/mnt/d"}])


def test_vpc_shape_limits():
    with pytest.raises(ValidationError):
        AgentSpec(**BASE, network={"subnets": [], "security_groups": ["sg-a"]})
    with pytest.raises(ValidationError):
        AgentSpec(**BASE, network={"subnets": ["subnet-a"], "security_groups": []})
