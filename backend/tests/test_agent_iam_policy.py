"""Per-agent execution-role derivation (T3).

The policy document is a pure function, so the contract is asserted in both
directions: a statement appears when the spec calls for it, and — the half that
actually delivers the isolation — is **absent** when it does not.

Nothing here proves a policy is sufficient. Only a live invoke does. See
implement.md's final section.
"""

import pytest

from app.schemas.agent import AgentSpec
from app.services import agent_iam
from app.services.agent_iam import RoleContext

CTX = RoleContext(
    account_id="123456789012",
    region="us-west-2",
    artifacts_bucket="launchpad-artifacts-123456789012-us-west-2",
    ecr_repo_arn="arn:aws:ecr:us-west-2:123456789012:repository/launchpad-agents",
    memory_id="launchpad_memory-abc123",
)


def _spec(**over) -> AgentSpec:
    base = {"name": "probe", "method": "zip_runtime", "system_prompt": "p"}
    return AgentSpec(**{**base, **over})


def _sids(spec: AgentSpec) -> set[str]:
    return {s["Sid"] for s in agent_iam.policy_document(spec, CTX)["Statement"]}


def _statement(spec: AgentSpec, sid: str) -> dict:
    for statement in agent_iam.policy_document(spec, CTX)["Statement"]:
        if statement["Sid"] == sid:
            return statement
    raise AssertionError(f"{sid} not emitted for this spec: {sorted(_sids(spec))}")


# ─── naming ──────────────────────────────────────────────────────────────────

class TestRoleName:
    def test_ordinary_name(self):
        name = agent_iam.role_name_for("support-bot", "abcdef1234567890")
        assert name == "launchpad-agent-support-bot-abcdef12"

    def test_stays_within_the_iam_limit(self):
        name = agent_iam.role_name_for("x" * 300, "abcdef1234567890")
        assert len(name) <= 64

    def test_names_sharing_a_long_prefix_do_not_collide(self):
        """Truncating the name alone would map both of these to one role — the id
        suffix is what makes truncation safe."""
        stem = "a-very-long-agent-name-that-will-certainly-be-truncated"
        first = agent_iam.role_name_for(stem + "-one", "1111111111")
        second = agent_iam.role_name_for(stem + "-two", "2222222222")
        assert first != second

    def test_the_agent_id_is_visible_in_the_name(self):
        """So an operator reading the IAM console can map a role to a ledger row."""
        assert "deadbeef" in agent_iam.role_name_for("bot", "deadbeefcafe")

    def test_unsafe_characters_are_sanitised(self):
        name = agent_iam.role_name_for("my agent/v2!", "abcdef12")
        assert name == "launchpad-agent-my-agent-v2-abcdef12"

    def test_an_empty_name_still_yields_a_valid_role(self):
        assert agent_iam.role_name_for("", "abcdef12").startswith("launchpad-agent-")


# ─── model scoping ───────────────────────────────────────────────────────────

class TestModelResources:
    def test_a_profile_id_authorizes_both_the_profile_and_the_model(self):
        """Invoking a profile authorizes against the profile ARN *and* the
        foundation models it fronts; scoping to one fails at first invoke."""
        resources = agent_iam.model_resources("global.anthropic.claude-sonnet-5", CTX)
        assert "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-5" in resources
        assert any("inference-profile/global.anthropic.claude-sonnet-5" in r for r in resources)

    @pytest.mark.parametrize("prefix", ["us.", "eu.", "apac."])
    def test_every_known_profile_prefix(self, prefix):
        resources = agent_iam.model_resources(f"{prefix}amazon.nova-2-lite-v1:0", CTX)
        assert any("inference-profile/" in r for r in resources)
        assert any(r.endswith("foundation-model/amazon.nova-2-lite-v1:0") for r in resources)

    def test_a_bare_foundation_model_id(self):
        assert agent_iam.model_resources("amazon.nova-2-lite-v1:0", CTX) == [
            "arn:aws:bedrock:*::foundation-model/amazon.nova-2-lite-v1:0"
        ]

    def test_an_unrecognised_id_falls_back_rather_than_guessing(self):
        """Custom model ids are a first-class feature and the valid id space cannot
        be enumerated — narrowing a shape we do not understand would break the agent
        instead of protecting it."""
        assert agent_iam.model_resources("my-private-endpoint", CTX) == [
            "arn:aws:bedrock:*::foundation-model/*"
        ]

    def test_an_empty_id_does_not_produce_a_broken_arn(self):
        assert agent_iam.model_resources("", CTX) == ["*"]


# ─── what a plain agent gets, and what it does not ───────────────────────────

class TestBaselineAgent:
    def test_gets_only_model_and_telemetry(self):
        assert _sids(_spec(memory={"short_term": False, "long_term": False})) == {
            "BedrockModels",
            "Telemetry",
            "TelemetryTracing",
        }

    def test_short_term_memory_is_on_by_default_so_memory_is_emitted(self):
        assert "AgentCoreMemory" in _sids(_spec())

    @pytest.mark.parametrize("absent", [
        "BedrockMantleInference",
        "MarketplaceOperationsFromBedrockMantleFor3pModels",
        "EcrPull",
        "EcrAuth",
        "SkillBundleObjects",
        "ManagedKbRetrieval",
        "ManagedKbAgenticRetrieval",
        "AgentCoreCodeInterpreter",
        "AgentCoreBrowser",
        "IdentityVaultSecrets",
        "A2AInvokePeerRuntimes",
    ])
    def test_unused_capabilities_are_not_granted(self, absent):
        """This is the isolation: an agent cannot reach a capability it never asked
        for, which is what the shared role handed to everyone."""
        assert absent not in _sids(_spec())

    def test_the_ab_test_orchestration_grant_is_gone(self):
        """16 actions including CreateGatewayRule / UpdateGateway / InvokeAgentRuntime
        were on every agent. That is what the platform does, from its own
        credentials — a compromised agent could rewrite gateway routing."""
        for spec in (_spec(), _spec(method="container"), _spec(method="harness")):
            assert "ABTestOrchestration" not in _sids(spec)


# ─── per-capability derivation ───────────────────────────────────────────────

class TestMantle:
    def test_mantle_adds_its_three_statements(self):
        sids = _sids(_spec(model_source="mantle"))
        assert "BedrockMantleInference" in sids
        assert "BedrockMantleCallWithBearerToken" in sids
        assert "MarketplaceOperationsFromBedrockMantleFor3pModels" in sids

    def test_the_marketplace_wildcard_keeps_its_guard_condition(self):
        """The CalledViaLast condition is what makes that wildcard acceptable."""
        statement = _statement(
            _spec(model_source="mantle"),
            "MarketplaceOperationsFromBedrockMantleFor3pModels",
        )
        assert statement["Condition"]["StringEquals"]["aws:CalledViaLast"] == (
            "bedrock-mantle.amazonaws.com"
        )


class TestMemory:
    def test_scoped_to_the_configured_memory(self):
        statement = _statement(_spec(), "AgentCoreMemory")
        assert statement["Resource"].endswith("memory/launchpad_memory-abc123")

    def test_falls_back_to_wildcard_when_the_memory_id_is_unknown(self):
        ctx = RoleContext(
            account_id=CTX.account_id, region=CTX.region,
            artifacts_bucket=CTX.artifacts_bucket, ecr_repo_arn=CTX.ecr_repo_arn,
        )
        doc = agent_iam.policy_document(_spec(), ctx)
        memory = next(s for s in doc["Statement"] if s["Sid"] == "AgentCoreMemory")
        assert memory["Resource"] == "*"

    def test_disabling_memory_drops_the_statement(self):
        spec = _spec(memory={"short_term": False, "long_term": False})
        assert "AgentCoreMemory" not in _sids(spec)


class TestKnowledgeBases:
    def test_retrieval_is_scoped_to_the_attached_kbs(self):
        spec = _spec(knowledge_bases=[{"kb_id": "KBONE"}, {"kb_id": "KBTWO"}])
        statement = _statement(spec, "ManagedKbRetrieval")
        assert statement["Resource"] == [
            "arn:aws:bedrock:us-west-2:123456789012:knowledge-base/KBONE",
            "arn:aws:bedrock:us-west-2:123456789012:knowledge-base/KBTWO",
        ]

    def test_agentic_retrieval_stays_wildcard_and_that_is_recorded(self):
        """AgenticRetrieveStream does not support resource scoping. Unchanged from
        the shared role, and documented rather than quietly narrowed."""
        spec = _spec(knowledge_bases=[{"kb_id": "KBONE"}])
        assert _statement(spec, "ManagedKbAgenticRetrieval")["Resource"] == "*"

    def test_kbs_imply_a_workload_token(self):
        spec = _spec(knowledge_bases=[{"kb_id": "KBONE"}])
        assert "AgentCoreWorkloadIdentity" in _sids(spec)


class TestSkills:
    def test_object_access_is_scoped_to_this_agents_skills(self):
        """The shared role granted skills/* — every agent could read every agent's
        bundles."""
        spec = _spec(skills=["skills/meeting-summarizer/", "skills/pdf-filler/"])
        statement = _statement(spec, "SkillBundleObjects")
        assert statement["Resource"] == [
            f"arn:aws:s3:::{CTX.artifacts_bucket}/skills/meeting-summarizer/*",
            f"arn:aws:s3:::{CTX.artifacts_bucket}/skills/pdf-filler/*",
        ]

    def test_an_s3_uri_is_normalised_to_a_prefix(self):
        spec = _spec(skills=[f"s3://{CTX.artifacts_bucket}/skills/alpha/"])
        statement = _statement(spec, "SkillBundleObjects")
        assert statement["Resource"] == [
            f"arn:aws:s3:::{CTX.artifacts_bucket}/skills/alpha/*"
        ]

    def test_the_list_condition_is_scoped_too(self):
        spec = _spec(skills=["skills/alpha/"])
        statement = _statement(spec, "SkillBundleList")
        assert statement["Condition"]["StringLike"]["s3:prefix"] == ["skills/alpha/*"]


class TestBuiltinTools:
    def test_code_interpreter_only_when_requested(self):
        spec = _spec(tools=[{"type": "builtin", "name": "code-interpreter"}])
        sids = _sids(spec)
        assert "AgentCoreCodeInterpreter" in sids
        assert "AgentCoreBrowser" not in sids

    def test_browser_only_when_requested(self):
        spec = _spec(tools=[{"type": "builtin", "name": "browser"}])
        sids = _sids(spec)
        assert "AgentCoreBrowser" in sids
        assert "AgentCoreCodeInterpreter" not in sids

    def test_a_gateway_tool_grants_a_workload_token_not_a_sandbox(self):
        spec = _spec(tools=[{"type": "gateway", "name": "hr_lookup"}])
        sids = _sids(spec)
        assert "AgentCoreWorkloadIdentity" in sids
        assert "IdentityVaultSecrets" in sids
        assert "AgentCoreCodeInterpreter" not in sids


class TestContainerMethod:
    def test_gets_ecr_pull_scoped_to_the_repo(self):
        statement = _statement(_spec(method="container"), "EcrPull")
        assert statement["Resource"] == [CTX.ecr_repo_arn]

    def test_zip_agents_do_not_get_ecr(self):
        assert "EcrPull" not in _sids(_spec(method="zip_runtime"))


class TestA2A:
    def test_an_a2a_agent_may_invoke_peer_runtimes(self):
        spec = _spec(protocol="a2a")
        statement = _statement(spec, "A2AInvokePeerRuntimes")
        assert statement["Resource"] == [
            "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/*"
        ]

    def test_an_http_agent_may_not(self):
        assert "A2AInvokePeerRuntimes" not in _sids(_spec(protocol="http"))


class TestTelemetry:
    def test_writes_are_scoped_to_the_runtime_log_groups(self):
        statement = _statement(_spec(), "Telemetry")
        assert all("/aws/bedrock-agentcore/runtimes/" in r for r in statement["Resource"])

    @pytest.mark.parametrize("dropped", [
        "logs:StartQuery",
        "logs:GetQueryResults",
        "logs:StopQuery",
        "logs:FilterLogEvents",
        "logs:GetLogEvents",
        "logs:DescribeLogGroups",
    ])
    def test_log_read_actions_are_gone(self, dropped):
        """Those are the console's read paths; they leaked onto the workload role."""
        actions = set()
        for statement in agent_iam.policy_document(_spec(), CTX)["Statement"]:
            action = statement["Action"]
            actions.update(action if isinstance(action, list) else [action])
        assert dropped not in actions


# ─── the mount policy must not be re-derived ─────────────────────────────────

class TestFsPolicyCharacterisation:
    """Byte-for-byte pin on the moved statements.

    The AWS devguide's example policy for this is wrong and incomplete; the shape
    below was established by IAM simulator plus live UpdateAgentRuntime probes. If
    someone "tidies" it, this test is what catches the regression.
    """

    def test_s3_files_shape(self):
        ap = (
            "arn:aws:s3files:us-west-2:123456789012:file-system/fs-abc"
            "/access-point/ap-1"
        )
        spec = _spec(
            method="container",
            filesystem={"s3_files": [{"access_point_arn": ap, "mount_path": "/mnt/a"}]},
            network={"subnets": ["subnet-1"], "security_groups": ["sg-1"]},
        )
        assert agent_iam.fs_policy_document(spec) == {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3files:ClientMount", "s3files:ClientWrite"],
                    "Resource": [
                        "arn:aws:s3files:us-west-2:123456789012:file-system/fs-abc"
                    ],
                    "Condition": {"ArnEquals": {"s3files:AccessPointArn": [ap]}},
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3files:GetAccessPoint"],
                    "Resource": [ap],
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3files:ListMountTargets"],
                    "Resource": [
                        "arn:aws:s3files:us-west-2:123456789012:file-system/fs-abc"
                    ],
                },
            ],
        }

    def test_efs_shape_keeps_the_conditioned_wildcard(self):
        ap = "arn:aws:elasticfilesystem:us-west-2:123456789012:access-point/fsap-1"
        spec = _spec(
            method="container",
            filesystem={"efs": [{"access_point_arn": ap, "mount_path": "/mnt/e"}]},
            network={"subnets": ["subnet-1"], "security_groups": ["sg-1"]},
        )
        assert agent_iam.fs_policy_document(spec) == {
            "Version": "2012-10-17",
            "Statement": [{
                "Effect": "Allow",
                "Action": [
                    "elasticfilesystem:ClientMount",
                    "elasticfilesystem:ClientWrite",
                ],
                "Resource": "*",
                "Condition": {
                    "ArnEquals": {"elasticfilesystem:AccessPointArn": [ap]}
                },
            }],
        }

    def test_no_mounts_yields_no_policy(self):
        assert agent_iam.fs_policy_document(_spec()) is None

    def test_the_policy_name_is_unchanged_so_a_migration_can_find_the_old_one(self):
        assert agent_iam.fs_policy_name("my-agent") == "launchpad-fs-my-agent"
