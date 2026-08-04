"""Per-agent role lifecycle against a stub IAM client (T3).

The provision stage is re-entered by `resume_pending_jobs()`, an agent can be
deleted and re-created under the same name, and a half-failed delete must not wedge
either. Those are the properties asserted here.
"""

import json

import pytest

from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import agent_iam
from app.services.agent_iam import RoleContext

CTX = RoleContext(
    account_id="123456789012",
    region="us-west-2",
    artifacts_bucket="bkt",
    ecr_repo_arn="arn:aws:ecr:us-west-2:123456789012:repository/launchpad-agents",
    memory_id="launchpad_memory-abc",
)


class EntityAlreadyExists(Exception):
    """Name-matched by agent_iam._is_already_exists."""


class NoSuchEntity(Exception):
    """Name-matched by agent_iam._is_no_such_entity."""


class StubIam:
    """Minimal in-memory IAM: roles → {trust, policies{name: doc}}."""

    def __init__(self, existing: dict | None = None, delete_role_fails: bool = False):
        self.roles: dict[str, dict] = dict(existing or {})
        self.delete_role_fails = delete_role_fails
        self.calls: list[str] = []

    def create_role(self, RoleName, AssumeRolePolicyDocument, **kwargs):
        self.calls.append(f"create_role:{RoleName}")
        if RoleName in self.roles:
            raise EntityAlreadyExists(RoleName)
        self.roles[RoleName] = {
            "trust": AssumeRolePolicyDocument, "policies": {}, "tags": kwargs.get("Tags")
        }
        return {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{RoleName}"}}

    def get_role(self, RoleName):
        self.calls.append(f"get_role:{RoleName}")
        if RoleName not in self.roles:
            raise NoSuchEntity(RoleName)
        return {"Role": {"Arn": f"arn:aws:iam::123456789012:role/{RoleName}"}}

    def update_assume_role_policy(self, RoleName, PolicyDocument):
        self.calls.append(f"update_trust:{RoleName}")
        self.roles[RoleName]["trust"] = PolicyDocument

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.calls.append(f"put_policy:{PolicyName}")
        self.roles[RoleName]["policies"][PolicyName] = json.loads(PolicyDocument)

    def delete_role_policy(self, RoleName, PolicyName):
        self.calls.append(f"delete_policy:{PolicyName}")
        if PolicyName not in self.roles.get(RoleName, {}).get("policies", {}):
            raise NoSuchEntity(PolicyName)
        del self.roles[RoleName]["policies"][PolicyName]

    def list_role_policies(self, RoleName):
        if RoleName not in self.roles:
            raise NoSuchEntity(RoleName)
        return {"PolicyNames": sorted(self.roles[RoleName]["policies"])}

    def delete_role(self, RoleName):
        self.calls.append(f"delete_role:{RoleName}")
        if self.delete_role_fails:
            raise RuntimeError("DeleteConflict: role has attached policies")
        if RoleName not in self.roles:
            raise NoSuchEntity(RoleName)
        del self.roles[RoleName]


S3_AP = "arn:aws:s3files:us-west-2:123456789012:file-system/fs-abc/access-point/ap-1"


def _agent(name="probe", agent_id="abcdef1234") -> Agent:
    return Agent(id=agent_id, name=name, method="zip_runtime", spec={}, version="1")


def _spec(**over) -> AgentSpec:
    base = {"name": "probe", "method": "zip_runtime", "system_prompt": "p"}
    return AgentSpec(**{**base, **over})


def _mount_spec() -> AgentSpec:
    return _spec(
        method="container",
        filesystem={"s3_files": [{"access_point_arn": S3_AP, "mount_path": "/mnt/a"}]},
        network={"subnets": ["subnet-1"], "security_groups": ["sg-1"]},
    )


class TestEnsureRole:
    def test_creates_the_role_and_its_capability_policy(self):
        iam = StubIam()
        agent = _agent()
        arn = agent_iam.ensure_role(iam, agent, _spec(), CTX)
        name = agent_iam.role_name_for(agent.name, agent.id)
        assert arn.endswith(f"role/{name}")
        assert agent_iam.capability_policy_name(agent.name) in iam.roles[name]["policies"]

    def test_tags_the_role_with_the_agent_id(self):
        """So an orphaned role can be swept and traced back to a ledger row."""
        iam = StubIam()
        agent = _agent()
        agent_iam.ensure_role(iam, agent, _spec(), CTX)
        tags = iam.roles[agent_iam.role_name_for(agent.name, agent.id)]["tags"]
        assert {"Key": agent_iam.MANAGED_TAG_KEY, "Value": agent.id} in tags

    def test_is_idempotent_so_a_resumed_job_converges(self):
        """resume_pending_jobs() re-runs provision from scratch."""
        iam = StubIam()
        agent = _agent()
        first = agent_iam.ensure_role(iam, agent, _spec(), CTX)
        second = agent_iam.ensure_role(iam, agent, _spec(), CTX)
        assert first == second
        assert len(iam.roles) == 1

    def test_adopts_an_existing_role_instead_of_failing(self):
        """A half-failed delete would otherwise wedge re-creating an agent under a
        name that was used before."""
        agent = _agent()
        name = agent_iam.role_name_for(agent.name, agent.id)
        iam = StubIam(existing={name: {"trust": "{}", "policies": {}, "tags": []}})
        arn = agent_iam.ensure_role(iam, agent, _spec(), CTX)
        assert arn.endswith(f"role/{name}")
        assert f"update_trust:{name}" in iam.calls

    def test_a_non_conflict_create_error_is_not_swallowed(self):
        class Boom(StubIam):
            def create_role(self, **kwargs):
                raise RuntimeError("MalformedPolicyDocument")

        with pytest.raises(RuntimeError, match="MalformedPolicyDocument"):
            agent_iam.ensure_role(Boom(), _agent(), _spec(), CTX)

    def test_the_trust_policy_names_agentcore_and_scopes_the_account(self):
        iam = StubIam()
        agent = _agent()
        agent_iam.ensure_role(iam, agent, _spec(), CTX)
        trust = json.loads(iam.roles[agent_iam.role_name_for(agent.name, agent.id)]["trust"])
        statement = trust["Statement"][0]
        assert statement["Principal"]["Service"] == "bedrock-agentcore.amazonaws.com"
        assert statement["Condition"]["StringEquals"]["aws:SourceAccount"] == "123456789012"
        assert "ArnEquals" not in statement["Condition"]  # no runtime ARN yet

    def test_a_runtime_arn_tightens_the_trust_condition(self):
        iam = StubIam()
        agent = _agent()
        runtime_arn = "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/probe-x"
        agent_iam.ensure_role(iam, agent, _spec(), CTX, runtime_arn=runtime_arn)
        trust = json.loads(iam.roles[agent_iam.role_name_for(agent.name, agent.id)]["trust"])
        assert trust["Statement"][0]["Condition"]["ArnEquals"]["aws:SourceArn"] == runtime_arn


class TestMountPolicyOnTheAgentsOwnRole:
    def test_mounts_land_on_the_agents_role_not_a_shared_one(self):
        iam = StubIam()
        agent = _agent()
        agent_iam.ensure_role(iam, agent, _mount_spec(), CTX)
        name = agent_iam.role_name_for(agent.name, agent.id)
        assert agent_iam.fs_policy_name(agent.name) in iam.roles[name]["policies"]

    def test_removing_mounts_on_republish_drops_the_policy(self):
        iam = StubIam()
        agent = _agent()
        agent_iam.ensure_role(iam, agent, _mount_spec(), CTX)
        agent_iam.ensure_role(iam, agent, _spec(), CTX)  # re-publish, mounts removed
        name = agent_iam.role_name_for(agent.name, agent.id)
        assert agent_iam.fs_policy_name(agent.name) not in iam.roles[name]["policies"]

    def test_a_shrinking_capability_set_is_reflected(self):
        """A re-publish that dropped knowledge bases must not leave the grant."""
        iam = StubIam()
        agent = _agent()
        agent_iam.ensure_role(iam, agent, _spec(knowledge_bases=[{"kb_id": "KB1"}]), CTX)
        agent_iam.ensure_role(iam, agent, _spec(), CTX)
        name = agent_iam.role_name_for(agent.name, agent.id)
        doc = iam.roles[name]["policies"][agent_iam.capability_policy_name(agent.name)]
        assert "ManagedKbRetrieval" not in {s["Sid"] for s in doc["Statement"]}


class TestDeleteRole:
    def test_removes_inline_policies_then_the_role(self):
        iam = StubIam()
        agent = _agent()
        agent_iam.ensure_role(iam, agent, _mount_spec(), CTX)
        assert agent_iam.delete_role(iam, agent) is True
        assert iam.roles == {}

    def test_an_absent_role_is_treated_as_already_gone(self):
        assert agent_iam.delete_role(StubIam(), _agent()) is True

    def test_a_failure_returns_false_without_raising(self):
        """Deleting an agent must not be blocked by IAM."""
        iam = StubIam(delete_role_fails=True)
        agent = _agent()
        agent_iam.ensure_role(iam, agent, _spec(), CTX)
        assert agent_iam.delete_role(iam, agent) is False

    def test_a_failure_logs_the_role_name_so_the_orphan_is_findable(self):
        iam = StubIam(delete_role_fails=True)
        agent = _agent()
        agent_iam.ensure_role(iam, agent, _spec(), CTX)
        logs: list[str] = []
        agent_iam.delete_role(iam, agent, logs.append)
        joined = "\n".join(logs)
        assert agent_iam.role_name_for(agent.name, agent.id) in joined
        assert agent_iam.MANAGED_TAG_KEY in joined

    def test_delete_then_recreate_under_the_same_name_works(self):
        iam = StubIam()
        agent = _agent()
        agent_iam.ensure_role(iam, agent, _spec(), CTX)
        agent_iam.delete_role(iam, agent)
        arn = agent_iam.ensure_role(iam, agent, _spec(), CTX)
        assert arn.endswith(agent_iam.role_name_for(agent.name, agent.id))


class TestIamPropagationRetry:
    @pytest.mark.parametrize("message", [
        "ValidationException: role is missing required permissions",
        "AccessDeniedException: User is not authorized to perform: sts:AssumeRole",
        "unable to assume the provided execution role",
        "AccessDenied",
    ])
    def test_recognises_each_observed_wording(self, message):
        """A brand-new role is a longer consistency window than a rewritten policy,
        and surfaces differently — the original predicate only matched the
        'missing required permissions' wording."""
        assert agent_iam.is_iam_propagation_error(RuntimeError(message)) is True

    def test_does_not_retry_an_unrelated_failure(self):
        assert agent_iam.is_iam_propagation_error(
            RuntimeError("ValidationException: containerUri is invalid")
        ) is False

    def test_retries_then_succeeds(self):
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("missing required permissions")
            return "ok"

        result = agent_iam.retry_iam_propagation(
            flaky, log=lambda _m: None, sleeper=lambda _s: None
        )
        assert result == "ok"
        assert attempts["n"] == 3

    def test_gives_up_after_the_attempt_budget(self):
        with pytest.raises(RuntimeError, match="missing required permissions"):
            agent_iam.retry_iam_propagation(
                lambda: (_ for _ in ()).throw(
                    RuntimeError("missing required permissions")
                ),
                log=lambda _m: None,
                attempts=2,
                sleeper=lambda _s: None,
            )

    def test_an_unrelated_error_is_raised_immediately(self):
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise RuntimeError("containerUri is invalid")

        with pytest.raises(RuntimeError, match="containerUri"):
            agent_iam.retry_iam_propagation(
                boom, log=lambda _m: None, sleeper=lambda _s: None
            )
        assert calls["n"] == 1  # no retry
