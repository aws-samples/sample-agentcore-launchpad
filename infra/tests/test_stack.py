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


def test_codebuild_is_arm64(template: Template):
    template.has_resource_properties(
        "AWS::CodeBuild::Project",
        {"Environment": Match.object_like({"Type": "ARM_CONTAINER"})},
    )


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
    with the exec role — without this the generated kb_search tool only ever
    returns AccessDeniedException."""
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
