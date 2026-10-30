import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stacks.base_stack import LaunchpadBaseStack


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App()
    stack = LaunchpadBaseStack(
        app, "launchpad-base", env=cdk.Environment(account="111111111111", region="us-west-2")
    )
    return Template.from_stack(stack)


@pytest.fixture(scope="module")
def east_template() -> Template:
    app = cdk.App()
    stack = LaunchpadBaseStack(
        app, "launchpad-base", env=cdk.Environment(account="111111111111", region="us-east-1")
    )
    return Template.from_stack(stack)


def test_core_resources_present(template: Template):
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.resource_count_is("AWS::ECR::Repository", 1)
    template.resource_count_is("AWS::CodeBuild::Project", 1)
    # Cognito User Pools do not exist in aws-cn; the gateway's inbound auth is
    # AWS_IAM instead (gateway_bootstrap.ensure_gateway).
    template.resource_count_is("AWS::Cognito::UserPool", 0)


def test_codebuild_is_arm64(template: Template):
    template.has_resource_properties(
        "AWS::CodeBuild::Project",
        {"Environment": Match.object_like({"Type": "ARM_CONTAINER"})},
    )


NJU = "https://mirror.nju.edu.cn/pypi/web/simple"
TENCENT = "https://mirrors.cloud.tencent.com/pypi/simple"


def _codebuild_env(template: Template) -> dict[str, str]:
    rendered = template.to_json()
    return {
        v["Name"]: v["Value"]
        for r in rendered["Resources"].values()
        if r["Type"] == "AWS::CodeBuild::Project"
        for v in r["Properties"]["Environment"]["EnvironmentVariables"]
    }


def test_codebuild_project_carries_the_cn_mirror_env_vars(template: Template):
    template.has_resource_properties(
        "AWS::CodeBuild::Project",
        {
            "Environment": Match.object_like(
                {
                    "EnvironmentVariables": Match.array_with(
                        [
                            Match.object_like({"Name": "PIP_INDEX_URL"}),
                            Match.object_like({"Name": "UV_INDEX_URL"}),
                            # uv 0.9.10 deprecates UV_INDEX_URL in favor of
                            # UV_DEFAULT_INDEX — both must carry the mirror.
                            Match.object_like({"Name": "UV_DEFAULT_INDEX"}),
                            Match.object_like({"Name": "PIP_FALLBACK_INDEX_URL"}),
                            Match.object_like({"Name": "NPM_CONFIG_REGISTRY"}),
                        ]
                    )
                }
            )
        },
    )


def test_codebuild_resolves_from_the_build_mirror_not_the_host_mirror(template: Template):
    """N11-r3-bug7: tuna answers CodeBuild's NAT egress with an immediate 4xx (both worker
    image builds died 0.2 s after "Looking in indexes") while the EC2 hosts get 200 from
    the same URL — so the builder's index is its own knob (`codebuild_pip_index_url`, NJU)
    with a fallback (`codebuild_pip_fallback_index_url`, Tencent); the host mirror
    (`pip_index_url`) never reaches the project."""
    env = _codebuild_env(template)
    assert env["PIP_INDEX_URL"] == env["UV_INDEX_URL"] == env["UV_DEFAULT_INDEX"] == NJU
    assert env["PIP_FALLBACK_INDEX_URL"] == TENCENT
    assert "tuna" not in str(env)

    app = cdk.App(context={
        "pip_index_url": "https://host-only.example.invalid/simple",
        "codebuild_pip_index_url": "https://build.example.invalid/simple",
        "codebuild_pip_fallback_index_url": "https://build-2.example.invalid/simple",
    })
    stack = LaunchpadBaseStack(
        app, "launchpad-base", env=cdk.Environment(account="111111111111", region="us-west-2")
    )
    env = _codebuild_env(Template.from_stack(stack))
    assert env["PIP_INDEX_URL"] == env["UV_DEFAULT_INDEX"] == "https://build.example.invalid/simple"
    assert env["PIP_FALLBACK_INDEX_URL"] == "https://build-2.example.invalid/simple"
    assert "host-only" not in str(env)


def test_optional_pip_extra_index_reaches_codebuild_only_when_set(template: Template):
    """N11-obs25: `-c pip_extra_index_url=…` adds PIP_EXTRA_INDEX_URL + UV_INDEX to the
    builder; the default synth carries neither (primary mirror only)."""
    rendered = template.to_json()
    names = [
        v["Name"]
        for r in rendered["Resources"].values()
        if r["Type"] == "AWS::CodeBuild::Project"
        for v in r["Properties"]["Environment"]["EnvironmentVariables"]
    ]
    assert "PIP_EXTRA_INDEX_URL" not in names and "UV_INDEX" not in names

    app = cdk.App(context={"pip_extra_index_url": "https://mirror.nju.edu.cn/pypi/web/simple"})
    stack = LaunchpadBaseStack(
        app, "launchpad-base", env=cdk.Environment(account="111111111111", region="us-west-2")
    )
    Template.from_stack(stack).has_resource_properties(
        "AWS::CodeBuild::Project",
        {
            "Environment": Match.object_like(
                {
                    "EnvironmentVariables": Match.array_with(
                        [
                            # array_with is order-sensitive: primary first, extras appended
                            Match.object_like({"Name": "PIP_INDEX_URL", "Value": NJU}),
                            Match.object_like({"Name": "PIP_EXTRA_INDEX_URL",
                                               "Value": "https://mirror.nju.edu.cn/pypi/web/simple"}),
                            Match.object_like({"Name": "UV_INDEX",
                                               "Value": "https://mirror.nju.edu.cn/pypi/web/simple"}),
                        ]
                    )
                }
            )
        },
    )


def test_ecr_scans_images_on_push(template: Template):
    """A deploy can only be blocked on findings if the push produced them (T10)."""
    template.has_resource_properties(
        "AWS::ECR::Repository",
        {
            "RepositoryName": "launchpad-agents",
            "ImageScanningConfiguration": Match.object_like({"ScanOnPush": True}),
        },
    )


def test_ecr_tags_stay_mutable(template: Template):
    """IMMUTABLE would break re-publish: the container path pushes the same
    `{agent}-v{version}` tag twice because packaging runs before the version is
    bumped. Deployment pins the image by digest instead, so the tag is cosmetic."""
    repos = template.find_resources("AWS::ECR::Repository")
    for repo in repos.values():
        assert repo["Properties"].get("ImageTagMutability") in (None, "MUTABLE")


def test_execution_role_trusts_agentcore(template: Template):
    template.has_resource_properties(
        "AWS::IAM::Role",
        Match.object_like(
            {
                "RoleName": "launchpad-agent-execution-role",
                "AssumeRolePolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        # N11-bug5: service principals are
                                        # partition-invariant; aws-cn IAM rejects
                                        # bedrock-agentcore.amazonaws.com.cn.
                                        "Principal": {
                                            "Service": "bedrock-agentcore.amazonaws.com"
                                        }
                                    }
                                )
                            ]
                        )
                    }
                ),
            }
        ),
    )


def test_non_legacy_region_uses_isolated_role_names(east_template: Template):
    for role_name in (
        "launchpad-agent-execution-role-us-east-1",
        "launchpad-gateway-role-us-east-1",
    ):
        east_template.has_resource_properties("AWS::IAM::Role", {"RoleName": role_name})


MANAGED_TAG = {"Key": "launchpad:managed", "Value": "true"}


@pytest.mark.parametrize(
    ("fixture_name", "role_name"),
    [
        ("template", "launchpad-agent-execution-role"),
        ("east_template", "launchpad-agent-execution-role-us-east-1"),
    ],
)
def test_execution_role_carries_managed_marker(request, fixture_name: str, role_name: str):
    """The backend trusts an execution role for additive grants only by its
    `launchpad:managed` tag (never by name); workspace roles created by the backend
    already carry it, so the stack-created default role must too — in both the legacy
    us-west-2 name and the regional-suffix name."""
    tpl: Template = request.getfixturevalue(fixture_name)
    tpl.has_resource_properties(
        "AWS::IAM::Role",
        {"RoleName": role_name, "Tags": Match.array_with([MANAGED_TAG])},
    )


def test_managed_marker_is_scoped_to_the_execution_role(template: Template):
    """No stack-wide tag: the gateway/KB/build roles must not read as managed."""
    roles = template.find_resources("AWS::IAM::Role")
    tagged = [
        props["Properties"]["RoleName"]
        for props in roles.values()
        if MANAGED_TAG in props["Properties"].get("Tags", [])
    ]
    assert tagged == ["launchpad-agent-execution-role"]
    assert len(roles) > 1


def test_execution_role_reads_skill_bundles(template: Template):
    """Harness runtimes fetch attached S3 skill bundles with the exec role —
    without skills/-scoped GetObject + ListBucket, invoke dies on AccessDenied."""
    template.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {"Sid": "SkillBundleObjects", "Action": "s3:GetObject"}
                                ),
                                Match.object_like(
                                    {
                                        "Sid": "SkillBundleList",
                                        "Action": "s3:ListBucket",
                                        "Condition": {
                                            "StringLike": {"s3:prefix": "skills/*"}
                                        },
                                    }
                                ),
                            ]
                        )
                    }
                )
            }
        ),
    )


def test_outputs_exported(template: Template):
    outputs = template.to_json()["Outputs"]
    for key in (
        "ArtifactsBucketName",
        "EcrRepoName",
        "EcrRepoUri",
        "CodeBuildProjectName",
        "AgentExecutionRoleArn",
    ):
        assert key in outputs, f"missing output {key}"
    assert "KbRoleArn" not in outputs


def test_no_bedrock_kb_or_memory_residue(template: Template):
    """R17 (C5): the Bedrock-managed-KB and AgentCore-Memory data-plane residue
    that N1 left in base_stack is deleted in N6 — the self-hosted substrate
    (launchpad-memory stack) replaces it entirely."""
    text = str(template.to_json())
    for needle in (
        "knowledge-base/",
        "AgenticRetrieveStream",
        "bedrock-agentcore:CreateEvent",
        "RetrieveMemoryRecords",
        "launchpad-kb-role",
    ):
        assert needle not in text


def test_no_hardcoded_partition_in_synth(template: Template):
    import json

    body = json.dumps(template.to_json())
    assert "arn:aws:" not in body
    # Service principal names are partition-invariant — lambda.amazonaws.com,
    # codebuild.amazonaws.com and apigateway.amazonaws.com keep this exact form
    # in aws-cn too (only resource ARNs/endpoints differ by partition). CDK's own
    # L2 constructs (codebuild.Project, lambda.Function, apigw.LambdaRestApi)
    # hardcode these as plain strings with no partition-aware Fn::Join, unlike
    # our own iam.ServicePrincipal(f"...{self.url_suffix}") calls — there is no
    # source line to cn-lint-allow-mark, so the same allowance is made here.
    allowed_principals = (
        "bedrock-agentcore.amazonaws.com",
        "lambda.amazonaws.com",
        "codebuild.amazonaws.com",
        "apigateway.amazonaws.com",
    )
    residual = body.replace(".amazonaws.com.cn", "")
    for principal in allowed_principals:
        residual = residual.replace(principal, "")
    assert ".amazonaws.com" not in residual


def test_office_facts_api_is_regional_not_edge(template: Template):
    # N11 r3 (live cn-northwest-1 failure): API Gateway's default EDGE endpoint
    # type does not exist in the aws-cn partition and rolled launchpad-base back.
    template.has_resource_properties(
        "AWS::ApiGateway::RestApi",
        {"Name": "launchpad-office-facts", "EndpointConfiguration": {"Types": ["REGIONAL"]}},
    )


def test_no_service_principal_is_built_from_the_url_suffix(template: Template):
    # N11-bug5 (live cn-northwest-1): IAM rejected the Fn::Join'd
    # "bedrock-agentcore.<URLSuffix>" principal ("Invalid principal in policy").
    import json

    body = json.dumps(template.to_json())
    assert '"bedrock-agentcore.", {"Ref": "AWS::URLSuffix"}' not in body
    assert "amazonaws.com.cn" not in json.dumps(
        [r["Properties"]["AssumeRolePolicyDocument"]
         for r in template.to_json()["Resources"].values()
         if r["Type"] == "AWS::IAM::Role"]
    )


def test_sample_lambdas_log_into_retention_bounded_groups(template: Template):
    """N11-obs56: every stack Lambda is pointed at an explicit log group with 90-day
    retention (CDK-named — never the service's `/aws/lambda/<fn>` name, which already
    exists on a stack deployed before this change), deleted with the stack."""
    groups = template.find_resources("AWS::Logs::LogGroup")
    assert len(groups) == 2
    for group in groups.values():
        assert group["Properties"]["RetentionInDays"] == 90
        assert "LogGroupName" not in group["Properties"]
        assert group["DeletionPolicy"] == "Delete"
    # The third AWS::Lambda::Function in the template is CDK's own S3 auto-delete-objects
    # custom-resource provider (a raw CfnResource with no log-group knob) — out of scope.
    samples = {
        fn["Properties"]["FunctionName"]: fn
        for fn in template.find_resources("AWS::Lambda::Function").values()
        if "FunctionName" in fn["Properties"]
    }
    assert set(samples) == {"launchpad-hr-database", "launchpad-office-facts"}
    for fn in samples.values():
        assert fn["Properties"]["LoggingConfig"]["LogGroup"]["Ref"] in groups


def _engine_key_deny(template: Template) -> dict:
    [policy] = template.find_resources("AWS::S3::BucketPolicy").values()
    [deny] = [s for s in policy["Properties"]["PolicyDocument"]["Statement"]
              if s.get("Sid") == "EngineKeyReadersOnly"]
    return deny


def test_engine_key_objects_are_readable_only_by_the_interceptor_and_hub_roles(template):
    """Per-user policy stream B (D2): an explicit Deny on policy/gateway/*/engine-key for every
    principal but the interceptor role and this environment's hub role — the BYOC CodeBuild
    project's `grant_read(<bucket>/*)` and other bucket-wide reads must not reach the key."""
    import json

    deny = _engine_key_deny(template)
    assert deny["Effect"] == "Deny" and deny["Principal"] == {"AWS": "*"}
    assert sorted(deny["Action"]) == ["s3:GetObject", "s3:GetObjectVersion"]
    assert "/policy/gateway/*/engine-key" in json.dumps(deny["Resource"])
    readers = json.dumps(deny["Condition"]["StringNotLike"]["aws:PrincipalArn"])
    assert ":role/launchpad-policy-interceptor" in readers
    assert ":role/launchpad-cn-ops-us-west-2" in readers
    assert "*" not in readers.replace('"*"', "")  # no wildcard reader


def test_engine_key_readers_follow_the_env_suffix():
    import json

    app = cdk.App(context={"env_name": "t1"})
    stack = LaunchpadBaseStack(
        app, "launchpad-base-t1", env=cdk.Environment(account="111111111111", region="us-west-2"))
    readers = json.dumps(_engine_key_deny(Template.from_stack(stack))["Condition"])
    assert "launchpad-policy-interceptor-t1" in readers
    assert "launchpad-cn-ops-us-west-2-t1" in readers


def _eval_tokens_deny(template: Template) -> dict:
    [policy] = template.find_resources("AWS::S3::BucketPolicy").values()
    [deny] = [s for s in policy["Properties"]["PolicyDocument"]["Statement"]
              if s.get("Sid") == "EvalTokensReadersOnly"]
    return deny


def test_eval_tokens_are_readable_only_by_the_eval_worker_and_hub_roles(template):
    """Per-user policy D5 (I9): an explicit Deny on evaluations/*/tokens.json (bearer
    principal tokens) for every principal but the evaluation worker's execution role and this
    environment's hub role — the bucket-wide readers (BYOC CodeBuild, workspace source
    reads) must not reach them. Same shape as EngineKeyReadersOnly."""
    import json

    deny = _eval_tokens_deny(template)
    assert deny["Effect"] == "Deny" and deny["Principal"] == {"AWS": "*"}
    assert sorted(deny["Action"]) == ["s3:GetObject", "s3:GetObjectVersion"]
    assert "/evaluations/*/tokens.json" in json.dumps(deny["Resource"])
    readers = json.dumps(deny["Condition"]["StringNotLike"]["aws:PrincipalArn"])
    assert ":role/launchpad-agent-launchpad-eval-worker-????????" in readers
    assert ":role/launchpad-cn-ops-us-west-2" in readers
    assert "*" not in readers.replace('"*"', "")  # no wildcard reader (only `?` per id char)
    # the engine-key Deny is untouched: two statements, one pattern
    assert _engine_key_deny(template)["Sid"] == "EngineKeyReadersOnly"


def test_eval_token_readers_follow_the_env_suffix():
    import json

    app = cdk.App(context={"env_name": "t1"})
    stack = LaunchpadBaseStack(
        app, "launchpad-base-t1", env=cdk.Environment(account="111111111111", region="us-west-2"))
    readers = json.dumps(_eval_tokens_deny(Template.from_stack(stack))["Condition"])
    assert ":role/launchpad-agent-launchpad-eval-worker-t1-????????-t1" in readers
    assert ":role/launchpad-cn-ops-us-west-2-t1" in readers
