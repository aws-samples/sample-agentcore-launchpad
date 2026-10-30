"""Per-agent execution-role derivation (T3).

The policy document is a pure function, so the contract is asserted in both
directions: a statement appears when the spec calls for it, and — the half that
actually delivers the isolation — is **absent** when it does not.

Nothing here proves a policy is sufficient. Only a live invoke does. See
implement.md's final section.
"""

import dataclasses
import json

import pytest

from app.schemas.agent import AgentSpec, ToolRef
from app.services import agent_iam
from app.services.agent_iam import RoleContext, policy_document

CTX = RoleContext(
    account_id="123456789012",
    region="us-west-2",
    artifacts_bucket="launchpad-artifacts-123456789012-us-west-2",
    ecr_repo_arn="arn:aws:ecr:us-west-2:123456789012:repository/launchpad-agents",
)
# A workspace whose self-hosted memory substrate exists (N6): the resource map carries
# the store id, snapshot bucket, extraction queue and OpenSearch domain.
MEM_CTX = dataclasses.replace(
    CTX,
    memory_store_id="launchpad_memory-default",
    memory_bucket="bkt",
    memory_queue_arn=CTX.arn("sqs", "launchpad-memory-extract"),
    memory_domain_arn=CTX.arn("es", "domain/lp-memory"),
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


# ─── what a plain agent gets, and what it does not ───────────────────────────

class TestBaselineAgent:
    def test_gets_only_telemetry_and_config_bundle_reads(self):
        # LaunchpadBundleRead is unconditional for a generated agent: its code always
        # reads _config_bundle() (routed A/B experiments), so the role always needs to
        # fetch the bundle JSON from the artifacts bucket (CN: S3, not the AgentCore
        # ConfigurationBundle service — plan §B.6).
        assert _sids(_spec(memory={"short_term": False, "long_term": False})) == {
            "ModelCredentialApiKey",
            "ModelCredentialVaultSecret",
            "LaunchpadBundleRead",
            "Telemetry",
            "TelemetryTracing",
        }

    @pytest.mark.parametrize("method", ["zip_runtime", "studio", "byoc", "harness"])
    def test_no_role_authorizes_a_model(self, method):
        """The Global models are reached with the workspace's API key (spec C3/C4);
        a bedrock:InvokeModel grant on an aws-cn role is dead weight."""
        over = {"method": method}
        if method == "studio":
            over.update(code="print(1)", studio_flow={"nodes": [], "edges": []})
        if method == "byoc":
            over.update(system_prompt="", byoc={"artifact_kind": "code_zip", "upload_id": "u1",
                                                "entrypoint": "main.py"})
        doc = agent_iam.policy_document(_spec(**over), CTX)
        actions = [a for s in doc["Statement"]
                   for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])]
        assert not any(a.startswith("bedrock:") for a in actions)
        assert "BedrockModels" not in {s["Sid"] for s in doc["Statement"]}

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
        for spec in (_spec(), _spec(method="harness")):
            assert "ABTestOrchestration" not in _sids(spec)


class TestModelCredential:
    """Every agent exchanges its workload token for the Global Bedrock API key."""

    def test_grants_the_exchange_on_the_exact_provider_and_own_identity(self):
        statement = _statement(_spec(), "ModelCredentialApiKey")
        assert set(statement["Action"]) == {
            "bedrock-agentcore:GetResourceApiKey", "bedrock-agentcore:GetWorkloadAccessToken",
        }
        base = "arn:aws:bedrock-agentcore:us-west-2:123456789012"
        # N-R6: every platform model credential provider is `launchpad-*` (Bedrock,
        # SiliconFlow, …) — a runtime fetches the one its model / judge needs
        assert statement["Resource"] == [
            f"{base}:token-vault/default",
            f"{base}:workload-identity-directory/default",
            agent_iam.own_workload_identity_arn(_spec(), CTX),
            f"{base}:token-vault/default/apikeycredentialprovider/launchpad-*",
        ]
        secret = _statement(_spec(), "ModelCredentialVaultSecret")
        assert secret["Action"] == ["secretsmanager:GetSecretValue"]
        assert secret["Resource"] == [
            "arn:aws:secretsmanager:us-west-2:123456789012:secret:"
            "bedrock-agentcore-identity!default/apikey/launchpad-*-*"
        ]

    def test_the_family_pattern_covers_every_platform_provider(self):
        from app.schemas.agent import providers

        for row in providers().values():
            assert row["credential_provider"].startswith("launchpad-")
        assert agent_iam.model_credential_providers("launchpad-global-bedrock") == ["launchpad-*"]
        assert agent_iam.model_credential_providers("launchpad-siliconflow") == ["launchpad-*"]

    def test_follows_the_specs_credential_name(self):
        """A custom (non-platform) provider name is granted explicitly, beside the family."""
        spec = _spec(model_credential="team-bedrock-key")
        api = _statement(spec, "ModelCredentialApiKey")["Resource"]
        assert api[-2].endswith("apikeycredentialprovider/team-bedrock-key")
        assert api[-1].endswith("apikeycredentialprovider/launchpad-*")
        secret = _statement(spec, "ModelCredentialVaultSecret")["Resource"]
        assert secret[0].endswith("apikey/team-bedrock-key-*")
        assert secret[1].endswith("apikey/launchpad-*-*")

    @pytest.mark.parametrize("over", [
        {}, {"method": "harness"},
        {"method": "studio", "code": "print(1)", "studio_flow": {"nodes": [], "edges": []}},
        {"method": "byoc", "system_prompt": "",
         "byoc": {"artifact_kind": "code_zip", "upload_id": "u1"}},
    ])
    def test_every_method_gets_it_and_the_partition_follows_the_context(self, over):
        cn = RoleContext(
            account_id="123456789012", region="cn-northwest-1",
            artifacts_bucket="b",
            ecr_repo_arn="arn:aws-cn:ecr:cn-northwest-1:123456789012:repository/r",
            partition="aws-cn", dns_suffix="amazonaws.com.cn",
        )
        doc = agent_iam.policy_document(_spec(**over), cn)
        sids = {s["Sid"] for s in doc["Statement"]}
        assert {"ModelCredentialApiKey", "ModelCredentialVaultSecret"} <= sids
        api = next(s for s in doc["Statement"] if s["Sid"] == "ModelCredentialApiKey")
        cn_base = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:"
        assert all(r.startswith(cn_base) for r in api["Resource"])


# ─── per-capability derivation ───────────────────────────────────────────────


class TestKnowledgeBases:
    """Bedrock Knowledge Bases have no CN implementation: the direct retrieval grants
    are gone (the self-hosted backend of N6 is reached through the gateway)."""

    def test_no_direct_retrieval_grant(self):
        spec = _spec(knowledge_bases=[{"kb_id": "KBONE"}, {"kb_id": "KBTWO"}])
        assert not {"ManagedKbRetrieval", "ManagedKbAgenticRetrieval"} & _sids(spec)

    def test_kbs_imply_a_workload_token(self):
        spec = _spec(knowledge_bases=[{"kb_id": "KBONE"}])
        assert "AgentCoreWorkloadIdentity" in _sids(spec)


class TestMemorySubstrate:
    """N6: S3 snapshot prefix, extraction queue and the memory index — only for agents
    with memory on, in a workspace whose substrate exists (ruling R2)."""

    def test_memory_agents_get_prefix_scoped_s3_sqs_and_es_grants(self):
        spec = _spec(memory={"short_term": True, "long_term": True})
        by = {s["Sid"]: s for s in agent_iam.policy_document(spec, MEM_CTX)["Statement"]}
        assert by["MemorySnapshotObjects"]["Resource"] == [
            CTX.arn("s3", "bkt/memory/launchpad_memory-default/*", region="", account="")
        ]
        assert set(by["MemorySnapshotObjects"]["Action"]) == {
            "s3:GetObject", "s3:PutObject", "s3:DeleteObject",
        }
        assert by["MemorySnapshotList"]["Resource"] == [
            CTX.arn("s3", "bkt", region="", account="")
        ]
        assert by["MemorySnapshotList"]["Condition"] == {
            "StringLike": {"s3:prefix": ["memory/launchpad_memory-default/*"]}
        }
        assert by["MemoryExtractionQueue"] == {
            "Sid": "MemoryExtractionQueue", "Effect": "Allow", "Action": ["sqs:SendMessage"],
            "Resource": [MEM_CTX.memory_queue_arn],
        }
        assert by["MemorySearch"]["Resource"] == [f"{MEM_CTX.memory_domain_arn}/launchpad-memory/*"]
        assert set(by["MemorySearch"]["Action"]) == {
            "es:ESHttpGet", "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpHead",
        }
        assert "KnowledgeSearch" not in by  # no KB mounted

    def test_a_pinned_store_scopes_the_prefix_and_kbs_get_their_own_index(self):
        spec = _spec(
            memory={"short_term": True, "memory_id": "team_notes-abcd1234"},
            knowledge_bases=[{"kb_id": "KB1"}, {"kb_id": "kbTwo"}],
        )
        by = {s["Sid"]: s for s in agent_iam.policy_document(spec, MEM_CTX)["Statement"]}
        assert by["MemorySnapshotObjects"]["Resource"] == [
            CTX.arn("s3", "bkt/memory/team_notes-abcd1234/*", region="", account="")
        ]
        assert by["MemorySnapshotList"]["Condition"]["StringLike"]["s3:prefix"] == [
            "memory/team_notes-abcd1234/*"
        ]
        assert by["KnowledgeSearch"]["Resource"] == [
            f"{MEM_CTX.memory_domain_arn}/launchpad-kb-kb1/*",
            f"{MEM_CTX.memory_domain_arn}/launchpad-kb-kbtwo/*",
        ]
        assert by["KnowledgeSearch"]["Action"] == ["es:ESHttpGet", "es:ESHttpPost"]

    def test_knowledge_search_needs_no_memory(self):
        spec = _spec(memory={"short_term": False, "long_term": False},
                     knowledge_bases=[{"kb_id": "KB1"}])
        sids = {s["Sid"] for s in agent_iam.policy_document(spec, MEM_CTX)["Statement"]}
        assert "KnowledgeSearch" in sids
        assert not {s for s in sids if s.startswith("Memory")}

    def test_without_a_substrate_no_memory_statement_appears(self):
        sids = _sids(_spec(memory={"short_term": True, "long_term": True},
                           knowledge_bases=[{"kb_id": "KB1"}]))
        assert not {s for s in sids if s.startswith("Memory") or s == "KnowledgeSearch"}

    def test_memory_off_gets_no_memory_statement_even_with_a_substrate(self):
        spec = _spec(memory={"short_term": False, "long_term": False})
        sids = {s["Sid"] for s in agent_iam.policy_document(spec, MEM_CTX)["Statement"]}
        assert not {s for s in sids if s.startswith("Memory")}

    def test_no_queue_means_no_queue_statement(self):
        ctx = dataclasses.replace(MEM_CTX, memory_queue_arn="")
        spec = _spec(memory={"short_term": True})
        sids = {s["Sid"] for s in agent_iam.policy_document(spec, ctx)["Statement"]}
        assert "MemoryExtractionQueue" not in sids
        assert {"MemorySnapshotObjects", "MemorySnapshotList", "MemorySearch"} <= sids

    def test_arns_follow_the_context_partition(self):
        cn = RoleContext(
            account_id="123456789012", region="cn-northwest-1", artifacts_bucket="b",
            ecr_repo_arn="arn:aws-cn:ecr:cn-northwest-1:123456789012:repository/r",
            partition="aws-cn", dns_suffix="amazonaws.com.cn",
        )
        cn = dataclasses.replace(
            cn, memory_store_id="launchpad_memory-ws1", memory_bucket="b",
            memory_queue_arn=cn.arn("sqs", "launchpad-memory-extract"),
            memory_domain_arn=cn.arn("es", "domain/lp-memory"),
        )
        spec = _spec(memory={"short_term": True, "long_term": True},
                     knowledge_bases=[{"kb_id": "KB1"}])
        by = {s["Sid"]: s for s in agent_iam.policy_document(spec, cn)["Statement"]}
        assert by["MemorySnapshotObjects"]["Resource"] == [
            "arn:aws-cn:s3:::b/memory/launchpad_memory-ws1/*"
        ]
        for sid in ("MemoryExtractionQueue", "MemorySearch", "KnowledgeSearch"):
            assert all(r.startswith("arn:aws-cn:") for r in by[sid]["Resource"]), sid

    def test_role_context_reads_the_substrate_from_the_resource_map(self):
        from tests.conftest import ws_ctx

        ws = ws_ctx(resources={
            "artifacts_bucket": "artifacts", "memory_id": "launchpad_memory-ws",
            "memory_queue_arn": "arn:aws-cn:sqs:cn-northwest-1:1:q",
            "memory_os_domain_arn": "arn:aws-cn:es:cn-northwest-1:1:domain/d",
        })
        ctx = agent_iam.role_context(ws)
        assert ctx.memory_store_id == "launchpad_memory-ws"
        assert ctx.memory_bucket == "artifacts"  # falls back to the artifacts bucket
        assert ctx.memory_queue_arn.endswith(":q")
        assert ctx.memory_domain_arn.endswith("domain/d")
        with_bucket = agent_iam.role_context(ws_ctx(resources={
            "artifacts_bucket": "artifacts", "memory_bucket": "snapshots",
        }))
        assert with_bucket.memory_bucket == "snapshots"
        assert with_bucket.memory_store_id == "" and with_bucket.memory_domain_arn == ""


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

    def test_a_registry_gateway_ref_grants_invoke_on_exactly_that_gateway(self):
        """Measured live: a Harness mounting an AWS_IAM Gateway signs with this
        role, and without InvokeGateway the Gateway answers 401. The region stays
        wildcarded — the Registry record may name a Gateway in another region."""
        spec = _spec(method="harness", tools=[
            {"type": "gateway", "name": "web-search",
             "config": {"record_id": "rec", "gateway_id": "web-search-smlhlkheht"}},
            {"type": "gateway", "name": "copy",
             "config": {"record_id": "rec2", "gateway_id": "web-search-smlhlkheht"}},
        ])
        statement = _statement(spec, "AgentCoreGatewayInvoke")
        assert statement["Action"] == ["bedrock-agentcore:InvokeGateway"]
        assert statement["Resource"] == [
            "arn:aws:bedrock-agentcore:*:123456789012:gateway/web-search-smlhlkheht"
        ]

    def test_gateway_invoke_is_absent_without_a_resolvable_gateway_id(self):
        """IAM's last line of defence (N17-obs487): creation-time validation now refuses a
        Gateway ref without ``config.gateway_id`` (``agent.gateway_tool_unbound`` — on CN
        AWS_IAM gateways a config-less ref is a broken agent, not a legacy OAuth one) and a
        malformed or foreign id; should a stored spec still carry one (a legacy row), the
        policy must never widen the Resource for it."""
        for gateway_id in (None, "*", "gw/*", "a-*", "web-search-smlhlkheht\n"):
            config = {"record_id": "rec", "gateway_id": gateway_id} if gateway_id else {}
            spec = _spec(tools=[{"type": "gateway", "name": "g", "config": config}])
            assert "AgentCoreGatewayInvoke" not in _sids(spec), gateway_id
        assert "AgentCoreGatewayInvoke" not in _sids(_spec())


class TestContainerMethod:
    def test_gets_ecr_pull_scoped_to_the_repo(self):
        statement = _statement(
            _spec(method="byoc", byoc={"artifact_kind": "container_source", "upload_id": "u1"}),
            "EcrPull",
        )
        assert statement["Resource"] == [CTX.ecr_repo_arn]

    def test_zip_agents_do_not_get_ecr(self):
        assert "EcrPull" not in _sids(_spec(method="zip_runtime"))


class TestA2A:
    def test_an_a2a_agent_may_invoke_peer_runtimes(self):
        spec = _spec(protocol="a2a")
        statement = _statement(spec, "A2AInvokePeerRuntimes")
        assert set(statement["Action"]) == set(agent_iam.RUNTIME_INVOKE_ACTIONS)  # N12-bug5
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
            method="zip_runtime",
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
            method="zip_runtime",
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


def test_zip_runtime_gateway_role_matches_the_harness_one_exactly():
    """Requirement B's first acceptance criterion: a zip_runtime agent with a
    gateway ToolRef must get the role it already got — this feature adds transport,
    not authorization. _uses_gateway() never reads spec.method, so the two policies
    must be identical — a self-built harness is Runtime-backed exactly like
    zip_runtime, including the routed-configuration-bundle grant."""
    tools = [{"type": "gateway", "name": "launchpad-gw"}]
    zip_spec = _spec(tools=tools)
    harness_spec = _spec(method="harness", tools=tools)

    zip_doc = agent_iam.policy_document(zip_spec, CTX)
    harness_doc = agent_iam.policy_document(harness_spec, CTX)
    assert zip_doc == harness_doc
    assert "AgentCoreWorkloadIdentity" in _sids(zip_spec)
    assert "IdentityVaultSecrets" in _sids(zip_spec)


def test_gateway_workload_statement_covers_both_token_mechanisms():
    """The generated client tries the injected workload token first and mints one
    with the execution role second. Both actions must already be granted, or the
    fallback silently cannot work."""
    statement = _statement(_spec(tools=[{"type": "gateway", "name": "launchpad-gw"}]),
                           "AgentCoreWorkloadIdentity")
    assert "bedrock-agentcore:GetWorkloadAccessToken" in statement["Action"]
    assert "bedrock-agentcore:GetResourceOauth2Token" in statement["Action"]


def test_gateway_tool_grants_scoped_invoke_gateway():
    spec = AgentSpec(
        name="gw-probe", method="zip_runtime", model_id="global.zai.glm-5.3", system_prompt="x",
        tools=[ToolRef(type="gateway", name="g", config={"gateway_id": "launchpad-gw-abc1234567"})],
    )
    ctx = RoleContext(
        account_id="123456789012", region="cn-northwest-1", artifacts_bucket="b",
        ecr_repo_arn="arn:aws-cn:ecr:cn-northwest-1:123456789012:repository/launchpad-agents",
        partition="aws-cn", dns_suffix="amazonaws.com.cn",
    )
    doc = policy_document(spec, ctx)
    stmt = next(s for s in doc["Statement"] if s["Sid"] == "AgentCoreGatewayInvoke")
    assert stmt["Action"] == ["bedrock-agentcore:InvokeGateway"]
    assert stmt["Resource"] == [
        "arn:aws-cn:bedrock-agentcore:*:123456789012:gateway/launchpad-gw-abc1234567"
    ]


def test_bare_gateway_tool_no_longer_needs_the_workload_token_family():
    """The AgentCoreWorkloadIdentity statement is for OAuth2/API-key token
    exchange (identity_tools.py.tmpl); a bare gateway ToolRef needs only
    AgentCoreGatewayInvoke now that the shared gateway is SigV4-only."""
    spec = AgentSpec(
        name="gw-probe", method="zip_runtime", model_id="global.zai.glm-5.3", system_prompt="x",
        tools=[ToolRef(type="gateway", name="g", config={"gateway_id": "launchpad-gw-abc1234567"})],
    )
    ctx = RoleContext(
        account_id="123456789012", region="cn-northwest-1", artifacts_bucket="b",
        ecr_repo_arn="arn:aws-cn:ecr:cn-northwest-1:123456789012:repository/launchpad-agents",
        partition="aws-cn", dns_suffix="amazonaws.com.cn",
    )
    doc = policy_document(spec, ctx)
    sids = {s["Sid"] for s in doc["Statement"]}
    assert "AgentCoreGatewayInvoke" in sids
    # AgentCoreWorkloadIdentity is still emitted today — _uses_gateway() grants the
    # family-wide token statement for ANY gateway ToolRef, not narrowed by this
    # node (see agent_iam.py:510's own comment: "Granting it for an OAuth Gateway
    # too is inert"). Documented as a candidate for a future tightening pass, not
    # an N3 change (narrowing _uses_gateway risks breaking an existing agent that
    # also has a KB or other MCP ref relying on the family-wide grant).
    assert "AgentCoreWorkloadIdentity" in sids


def test_no_gateway_tool_still_grants_no_workload_identity():
    assert "AgentCoreWorkloadIdentity" not in _sids(_spec())


def test_generated_agents_can_read_a_routed_launchpad_bundle():
    """CN (N12 E3-5): a routed arm's config bundle is immutable JSON under
    ``bundles/<agent>/<id>/v<n>.json`` in the artifacts bucket; the runtime's
    ``_config_bundle()`` fetches it with its own role, so every per-agent role carries
    a read-only grant on exactly that prefix. No ``ConfigurationBundle*`` action is
    granted anywhere (the service does not exist in aws-cn)."""
    statement = _statement(_spec(), "LaunchpadBundleRead")
    assert statement["Action"] == ["s3:GetObject"]
    assert statement["Resource"] == [
        f"arn:aws:s3:::{CTX.artifacts_bucket}/bundles/*"
    ]
    for method in ("zip_runtime", "studio", "harness"):
        assert "LaunchpadBundleRead" in _sids(_spec(method=method))
    doc = agent_iam.policy_document(_spec(), CTX)
    assert "ConfigurationBundle" not in json.dumps(doc)
    # no artifacts bucket (un-bootstrapped workspace) → no dangling grant
    bare = dataclasses.replace(CTX, artifacts_bucket="")
    sids = {s["Sid"] for s in agent_iam.policy_document(_spec(), bare)["Statement"]}
    assert "LaunchpadBundleRead" not in sids


# ─── N12 E1-3b: the preset hook puts the worker statements on the worker's role ─────

class TestEvalWorkerPresetHook:
    CN_CTX = RoleContext(
        account_id="111122223333", region="cn-northwest-1",
        artifacts_bucket="launchpad-artifacts-cn", ecr_repo_arn="arn:aws-cn:ecr:x",
        partition="aws-cn", dns_suffix="amazonaws.com.cn",
    )

    def _worker(self):
        from types import SimpleNamespace

        from app.system_agents.presets import EVAL_WORKER, build_spec

        spec = build_spec(EVAL_WORKER, "launchpad-artifacts-cn", EVAL_WORKER.default_options())
        agent = SimpleNamespace(system_key=EVAL_WORKER.key, name=spec.name, id="agt1")
        return agent, spec

    def test_eval_worker_statements(self):
        """The preset's ``extra_iam`` is exactly ``agent_iam.eval_worker_statements`` (E3-5),
        appended after the spec-derived policy."""
        agent, spec = self._worker()
        extra = agent_iam.preset_statements(agent, spec, self.CN_CTX)
        assert extra == agent_iam.eval_worker_statements(self.CN_CTX)
        doc = agent_iam.policy_document(spec, self.CN_CTX, extra_statements=extra)
        by_sid = {s["Sid"]: s for s in doc["Statement"]}
        assert {"EvalWorkerSpansRead", "EvalWorkerArtifacts", "EvalWorkerArtifactsList",
                "EvalWorkerInvokeTargets", "ModelCredentialApiKey"} <= set(by_sid)
        assert "EvalWorkerCodeEvaluators" not in by_sid  # code evaluators off by default
        assert by_sid["EvalWorkerInvokeTargets"]["Condition"] == {
            "StringEquals": {"aws:ResourceAccount": "111122223333"}}
        assert set(by_sid["EvalWorkerInvokeTargets"]["Action"]) == {
            "bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser"}
        assert not any("arn:aws:" in r for st in doc["Statement"]
                       for r in ([st["Resource"]] if isinstance(st["Resource"], str)
                                 else st["Resource"]))

    def test_opt_in_statements_follow_the_settings(self, monkeypatch):
        from app.core.config import get_settings
        from app.templates import eval_worker as ew

        agent, spec = self._worker()
        monkeypatch.setenv("LAUNCHPAD_EVAL_CODE_EVALUATORS_ENABLED", "1")
        monkeypatch.setenv("LAUNCHPAD_EVAL_RESULTS_LOG_GROUP_PREFIX",
                           "/launchpad/evaluations/results/")
        get_settings.cache_clear()
        try:
            sids = {s["Sid"] for s in ew.preset_iam_statements(self.CN_CTX, spec)}
        finally:
            get_settings.cache_clear()
        assert {"EvalWorkerCodeEvaluators", "EvalWorkerResultsMirror"} <= sids

    def test_ordinary_agents_get_no_preset_statements(self):
        from types import SimpleNamespace

        spec = _spec()
        assert agent_iam.preset_statements(SimpleNamespace(system_key=None), spec, CTX) == []
        doc = agent_iam.policy_document(spec, CTX)
        assert not any(s["Sid"].startswith("EvalWorker") for s in doc["Statement"])

    def test_ensure_role_puts_the_preset_statements_on_the_role(self):
        import json as _json
        from unittest.mock import MagicMock

        agent, spec = self._worker()
        iam = MagicMock()
        iam.create_role.return_value = {"Role": {"Arn": "arn:aws-cn:iam::111122223333:role/r"}}
        agent_iam.ensure_role(iam, agent, spec, self.CN_CTX)
        policy = _json.loads(iam.put_role_policy.call_args.kwargs["PolicyDocument"])
        assert "EvalWorkerInvokeTargets" in {s["Sid"] for s in policy["Statement"]}

    def _existing_role_iam(self, stored_caps: dict | None):
        """IAM as the repair path sees it: the role exists (CreateRole → EntityAlreadyExists),
        the trust document is current, and the caps inline policy is ``stored_caps``."""
        import json as _json
        from unittest.mock import MagicMock

        agent, spec = self._worker()
        trust = agent_iam.trust_policy(self.CN_CTX, None)
        iam = MagicMock()
        iam.create_role.side_effect = type("EntityAlreadyExists", (Exception,), {})("r")
        iam.get_role.return_value = {"Role": {
            "Arn": "arn:aws-cn:iam::111122223333:role/r",
            "AssumeRolePolicyDocument": _json.loads(_json.dumps(trust)),
        }}
        if stored_caps is None:
            iam.get_role_policy.side_effect = type("NoSuchEntity", (Exception,), {})("p")
        else:
            iam.get_role_policy.return_value = {"PolicyDocument": stored_caps}
        return iam, agent, spec

    def test_existing_worker_role_gets_a_changed_caps_policy_re_put_on_provision(self):
        """N12-bug5 item 2: the live worker role carried the pre-fix statement
        (InvokeAgentRuntime only). A repair / redeploy re-enters provision → ensure_role
        adopts the role and re-puts the caps inline policy because the rendered document
        differs — no reinstall needed. Byte-identical stored policy → no PutRolePolicy."""
        import json as _json

        agent, spec = self._worker()
        current = agent_iam.policy_document(
            spec, self.CN_CTX,
            extra_statements=agent_iam.preset_statements(agent, spec, self.CN_CTX))
        stale = _json.loads(_json.dumps(current))
        for st in stale["Statement"]:
            if st["Sid"] == "EvalWorkerInvokeTargets":
                st["Action"] = ["bedrock-agentcore:InvokeAgentRuntime"]  # what t1 had
        assert stale != current

        iam, agent, spec = self._existing_role_iam(stale)
        lines: list[str] = []
        agent_iam.ensure_role(iam, agent, spec, self.CN_CTX, log=lines.append)
        iam.create_role.assert_called_once()
        iam.update_assume_role_policy.assert_not_called()  # trust unchanged (N10v2-bug1)
        put = iam.put_role_policy.call_args_list[0].kwargs
        assert put["RoleName"] == agent_iam.role_name_for(agent.name, agent.id)
        assert put["PolicyName"] == agent_iam.capability_policy_name(agent.name)
        written = _json.loads(put["PolicyDocument"])
        (invoke,) = [s for s in written["Statement"] if s["Sid"] == "EvalWorkerInvokeTargets"]
        assert "bedrock-agentcore:InvokeAgentRuntimeForUser" in invoke["Action"]
        assert any("reusing existing execution role" in line for line in lines)

        # idempotent: the stored document already equals the rendered one → nothing written
        iam2, agent, spec = self._existing_role_iam(_json.loads(_json.dumps(written)))
        lines2: list[str] = []
        agent_iam.ensure_role(iam2, agent, spec, self.CN_CTX, log=lines2.append)
        caps_puts = [c for c in iam2.put_role_policy.call_args_list
                     if c.kwargs["PolicyName"] == agent_iam.capability_policy_name(agent.name)]
        assert caps_puts == []
        assert any("unchanged" in line for line in lines2)


def test_no_role_in_the_cn_tree_carries_the_ab_test_or_bundle_service_grants():
    """C5: the Global ``ABTestOrchestration`` / ``ConfigurationBundleRead`` Sids are
    deleted, not flagged — neither the per-agent policy, the workspace-role mirror nor
    the CDK base stack may name them."""
    from pathlib import Path

    root = Path(agent_iam.__file__).resolve().parents[3]
    sources = [
        Path(agent_iam.__file__).read_text(),
        (root / "backend/app/services/workspace_iam.py").read_text(),
        (root / "infra/stacks/base_stack.py").read_text(),
    ]
    for text in sources:
        assert "ABTestOrchestration" not in text
        assert "GetConfigurationBundle" not in text


# ─── the evaluation worker's execution role (N12 E3-5; plan §B.0 "Worker execution role") ─

def test_eval_worker_statements_cover_spans_artifacts_and_replay_only():
    statements = agent_iam.eval_worker_statements(CTX)
    by_sid = {s["Sid"]: s for s in statements}
    assert set(by_sid) == {
        "EvalWorkerSpansRead", "EvalWorkerArtifacts", "EvalWorkerArtifactsList",
        "EvalWorkerInvokeTargets",
    }
    spans = by_sid["EvalWorkerSpansRead"]
    assert set(spans["Action"]) == {
        "logs:StartQuery", "logs:GetQueryResults", "logs:StopQuery",
        "logs:FilterLogEvents", "logs:DescribeLogGroups",
    }
    assert spans["Resource"] == [
        "arn:aws:logs:us-west-2:123456789012:log-group:aws/spans",
        "arn:aws:logs:us-west-2:123456789012:log-group:aws/spans:*",
        "arn:aws:logs:us-west-2:123456789012:log-group:/aws/bedrock-agentcore/runtimes/*",
    ]
    artifacts = by_sid["EvalWorkerArtifacts"]
    assert set(artifacts["Action"]) == {"s3:GetObject", "s3:PutObject", "s3:DeleteObject"}
    bucket = f"arn:aws:s3:::{CTX.artifacts_bucket}"
    assert artifacts["Resource"] == [
        f"{bucket}/evaluations/*", f"{bucket}/eval-datasets/*", f"{bucket}/bundles/*",
    ]
    listing = by_sid["EvalWorkerArtifactsList"]
    assert listing["Action"] == ["s3:ListBucket"] and listing["Resource"] == [bucket]
    assert listing["Condition"] == {"StringLike": {"s3:prefix": [
        "evaluations/*", "eval-datasets/*", "bundles/*"]}}


def test_eval_worker_invoke_grant_is_runtime_wide_but_account_bound():
    """Lead ruling (3) / R-15: ``InvokeAgentRuntime`` on ``runtime/*`` — runtime names
    are ``sanitize_runtime_name(agent.name)``, so no name-prefix narrowing exists — with
    ``aws:ResourceAccount`` pinned to this account; per-job session-policy narrowing is
    the recorded follow-up."""
    (invoke,) = [s for s in agent_iam.eval_worker_statements(CTX)
                 if s["Sid"] == "EvalWorkerInvokeTargets"]
    # N12-bug5: the worker sends runtimeUserId on every replay, so the data plane also
    # authorizes the ForUser action — both, same resources, same account condition
    assert invoke["Action"] == [
        "bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser"]
    assert invoke["Resource"] == [
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/*"]
    assert invoke["Condition"] == {"StringEquals": {"aws:ResourceAccount": "123456789012"}}
    assert "bedrock-agentcore:StopRuntimeSession" not in invoke["Action"]


def test_eval_worker_optional_grants_are_off_by_default():
    base = {s["Sid"] for s in agent_iam.eval_worker_statements(CTX)}
    assert "EvalWorkerCodeEvaluators" not in base and "EvalWorkerResultsMirror" not in base
    with_code = {s["Sid"]: s for s in agent_iam.eval_worker_statements(
        CTX, code_evaluators=True)}
    assert with_code["EvalWorkerCodeEvaluators"]["Action"] == ["lambda:InvokeFunction"]
    assert with_code["EvalWorkerCodeEvaluators"]["Resource"] == [
        "arn:aws:lambda:us-west-2:123456789012:function:launchpad-evalfn-*"]
    with_mirror = {s["Sid"]: s for s in agent_iam.eval_worker_statements(
        CTX, results_mirror_prefix="/launchpad/evaluations/results")}
    mirror = with_mirror["EvalWorkerResultsMirror"]
    assert set(mirror["Action"]) == {"logs:CreateLogGroup", "logs:CreateLogStream",
                                     "logs:PutLogEvents"}
    assert mirror["Resource"] == [
        "arn:aws:logs:us-west-2:123456789012:log-group:/launchpad/evaluations/results/*",
        "arn:aws:logs:us-west-2:123456789012:log-group:/launchpad/evaluations/results/*:*",
    ]


def test_eval_worker_statements_are_partition_aware_and_need_the_bucket():
    cn = dataclasses.replace(CTX, partition="aws-cn", region="cn-northwest-1",
                             dns_suffix="amazonaws.com.cn")  # cn-lint: allow
    doc = json.dumps(agent_iam.eval_worker_statements(cn))
    assert "arn:aws:" not in doc and "arn:aws-cn:" in doc
    with pytest.raises(ValueError, match="artifacts_bucket"):
        agent_iam.eval_worker_statements(dataclasses.replace(CTX, artifacts_bucket=""))
