"""BYO-mount IAM inline policy — document shape + provision/delete lifecycle."""


from app.deployer import container as c
from app.schemas.agent import AgentSpec

S3_AP = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-abc/access-point/ap-1"
S3_AP2 = "arn:aws:s3files:us-west-2:111122223333:file-system/fs-abc/access-point/ap-2"
EFS_AP = "arn:aws:elasticfilesystem:us-west-2:111122223333:access-point/fsap-0123"
VPC = {"subnets": ["subnet-a"], "security_groups": ["sg-1"]}


class StubIam:
    def __init__(self):
        self.put_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    def put_role_policy(self, **kw):
        self.put_calls.append(kw)

    def delete_role_policy(self, **kw):
        self.delete_calls.append(kw)


def _spec(**over) -> AgentSpec:
    return AgentSpec(name="fs-agent", method="container", system_prompt="hi", **over)


class AgentRow:
    id = "agent-1"
    name = "fs-agent"
    method = "container"
    resource_id = "fs_agent-abc123"
    spec = {"name": "fs-agent", "method": "container", "system_prompt": "hi"}


# The policy-document, fs-sync and IAM-propagation-retry tests moved to
# tests/test_agent_iam_policy.py and tests/test_agent_iam_lifecycle.py along with the
# code itself (app/services/agent_iam.py). What stays here is the container path's own
# integration with it.


def test_delete_agent_resources_drops_the_stale_shared_role_policy(monkeypatch):
    class StubControlClient:
        class exceptions:
            class ResourceNotFoundException(Exception):
                pass

        def delete_agent_runtime(self, agentRuntimeId):
            pass

    monkeypatch.setattr(c, "control_client", lambda: StubControlClient())
    monkeypatch.setattr(c.rt, "delete_runtime", lambda cl, rid: None)

    class Settings:
        region = "us-west-2"
        # The shared-role fallback: with per-agent roles on, the whole role is
        # deleted by routers/agents.py and this cleanup is unnecessary.
        per_agent_execution_roles = False
        resources = {"execution_role_arn": "arn:aws:iam::1:role/launchpad-base"}

    monkeypatch.setattr(c, "get_settings", lambda: Settings())
    iam = StubIam()
    c.delete_agent_resources(AgentRow(), iam_client=iam)
    (call,) = iam.delete_calls
    assert call == {"RoleName": "launchpad-base", "PolicyName": "launchpad-fs-fs-agent"}
