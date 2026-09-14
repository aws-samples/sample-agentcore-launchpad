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
    template.resource_count_is("AWS::Cognito::UserPool", 1)


def test_cognito_groups_and_users(template: Template):
    template.resource_count_is("AWS::Cognito::UserPoolGroup", 2)
    template.resource_count_is("AWS::Cognito::UserPoolUser", 2)
    template.has_resource_properties(
        "AWS::Cognito::UserPoolGroup", {"GroupName": "platform-admin"}
    )
    template.has_resource_properties("AWS::Cognito::UserPoolGroup", {"GroupName": "hr-analyst"})
    # Demo identity rename (issue #17): the platform-admin demo user is `admin`.
    template.has_resource_properties("AWS::Cognito::UserPoolUser", {"Username": "admin"})
    template.has_resource_properties("AWS::Cognito::UserPoolUser", {"Username": "demo"})


def test_codebuild_is_arm64(template: Template):
    template.has_resource_properties(
        "AWS::CodeBuild::Project",
        {"Environment": Match.object_like({"Type": "ARM_CONTAINER"})},
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
        "launchpad-kb-role-us-east-1",
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


def test_execution_role_can_read_custom_evaluators_for_ab_tests(template: Template):
    """AgentCore assumes this role to resolve customer-owned evaluators."""
    template.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Sid": "ABTestOrchestration",
                                        "Action": Match.array_with(
                                            [
                                                "bedrock-agentcore:ListConfigurationBundleVersions",
                                                "bedrock-agentcore:GetEvaluator",
                                            ]
                                        ),
                                    }
                                )
                            ]
                        )
                    }
                )
            }
        ),
    )


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


def test_execution_role_can_retrieve_managed_kbs(template: Template):
    """zip_runtime/container agents mount KBs by calling the Bedrock data plane
    with the exec role — without these the generated kb_search / kb_deep_search
    tools only ever return AccessDeniedException. AgenticRetrieveStream is not
    resource-scopable, hence the deliberate '*'."""
    template.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Sid": "ManagedKbRetrieval",
                                        "Action": [
                                            "bedrock:Retrieve",
                                            "bedrock:GetKnowledgeBase",
                                        ],
                                    }
                                ),
                                Match.object_like(
                                    {
                                        "Sid": "ManagedKbAgenticRetrieval",
                                        "Action": "bedrock:AgenticRetrieveStream",
                                        "Resource": "*",
                                    }
                                ),
                            ]
                        )
                    }
                )
            }
        ),
    )


def test_execution_role_can_run_bedrock_mantle_inference(template: Template):
    """Bedrock Mantle is a separate IAM service from bedrock — bedrock:InvokeModel
    does not cover it, so a model_source="mantle" agent reaches ACTIVE and then
    fails its first invoke with `401 access_denied … bedrock-mantle:CreateInference`.
    CallWithBearerToken is equally required: auth is a short-lived bearer token
    minted from this role, not SigV4 on the request itself."""
    template.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Sid": "BedrockMantleInference",
                                        "Action": [
                                            "bedrock-mantle:Get*",
                                            "bedrock-mantle:List*",
                                            "bedrock-mantle:CreateInference",
                                        ],
                                    }
                                ),
                                Match.object_like(
                                    {
                                        "Sid": "BedrockMantleCallWithBearerToken",
                                        "Action": "bedrock-mantle:CallWithBearerToken",
                                        "Resource": "*",
                                    }
                                ),
                                Match.object_like(
                                    {
                                        "Sid": (
                                            "MarketplaceOperationsFromBedrockMantleFor3pModels"
                                        ),
                                        "Condition": {
                                            "StringEquals": {
                                                "aws:CalledViaLast": (
                                                    "bedrock-mantle.amazonaws.com"
                                                )
                                            }
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
        "UserPoolId",
        "UserPoolClientId",
        "AgentExecutionRoleArn",
    ):
        assert key in outputs, f"missing output {key}"


def test_gateway_role_can_resolve_a_routed_config_bundle(template: Template):
    """A request carrying config-bundle baggage makes the GATEWAY fetch the bundle
    with its own role, not just the agent. Measured live: without this grant the
    Gateway answers the MCP call with HTTP 400 'Config bundle fetch failed: … not
    authorized to perform: bedrock-agentcore:GetConfigurationBundleVersion', so an
    agent under a config-bundle A/B silently loses every Gateway tool."""
    template.has_resource_properties(
        "AWS::IAM::Policy",
        Match.object_like(
            {
                "PolicyDocument": Match.object_like(
                    {
                        "Statement": Match.array_with(
                            [
                                Match.object_like(
                                    {
                                        "Sid": "ConfigurationBundleRead",
                                        "Action": [
                                            "bedrock-agentcore:GetConfigurationBundle",
                                            "bedrock-agentcore:GetConfigurationBundleVersion",
                                        ],
                                    }
                                )
                            ]
                        )
                    }
                )
            }
        ),
    )
