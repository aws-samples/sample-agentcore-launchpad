"""The spoke role template (`infra/spoke/launchpad-workspace-role.yaml`).

It is plain CloudFormation, deployed by an Organization admin into another
account, so nothing else in `make verify` reads it at all. This suite parses the
file rather than pattern-matching it, by handing it to CDK's own template loader
(`CfnInclude`), which is the same parser `cdk deploy` uses. No YAML dependency is
added: aws-cdk-lib brings the parser.

What that parse actually catches (measured, not assumed): malformed YAML, and an
intrinsic referencing something the template does not declare (`Ref` to a missing
parameter). What it does NOT catch: an unknown resource *property*, and — the one
worth stating out loud — a **misspelled action name**. IAM itself accepts
`s3:PutObjct` and simply never matches it, so no offline check can flag it; only
the live bootstrap can, by denying the call that needed it.

The assertions therefore aim at the properties that make the template *safe*
rather than merely valid: the trust policy's exactly-two conditions, PassRole's
PassedToService condition, and the ARN scoping of the statements that can be
scoped (a `Resource: "*"` sneaking into one of those is the regression this
catches). They read the parsed template by key, which is also what turns a typo
in one of those property names into a failure here.
"""

from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk import cloudformation_include as cfn_include
from aws_cdk.assertions import Template

TEMPLATE = Path(__file__).resolve().parents[1] / "spoke" / "launchpad-workspace-role.yaml"

# Statements whose resources Launchpad's naming discipline makes scopable. The
# rest are `*` by necessity (documented per statement in the template); a bare
# `*` appearing in one of THESE is the mistake worth failing on.
SCOPED_SIDS = frozenset(
    {
        "IamOnLaunchpadRoles",
        "PassLaunchpadRoles",
        "S3Launchpad",
        "EcrLaunchpad",
        "CodeBuildLaunchpad",
    }
)


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    """The template as CloudFormation will see it.

    CDK adds its own `BootstrapVersion` parameter and `CheckBootstrapVersion`
    rule to the synthesized stack; the assertions below name what they read, so
    those extras are simply ignored.
    """
    app = cdk.App()
    stack = cdk.Stack(app, "spoke")
    cfn_include.CfnInclude(stack, "SpokeRole", template_file=str(TEMPLATE))
    return Template.from_stack(stack).to_json()


@pytest.fixture(scope="module")
def role(template: dict[str, Any]) -> dict[str, Any]:
    roles = [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::IAM::Role"
    ]
    assert len(roles) == 1, "the template provisions exactly one role and nothing else"
    return roles[0]["Properties"]


@pytest.fixture(scope="module")
def statements(role: dict[str, Any]) -> list[dict[str, Any]]:
    policies = role["Policies"]
    assert len(policies) == 1
    return policies[0]["PolicyDocument"]["Statement"]


def test_parameters_constrain_what_they_accept(template: dict[str, Any]):
    """A mistyped ARN or a one-character ExternalId must be refused by
    CloudFormation, not accepted into a trust policy nobody re-reads."""
    params = template["Parameters"]
    assert params["HubRoleArn"]["AllowedPattern"] == "^arn:aws[a-z-]*:iam::\\d{12}:role/.+$"
    external = params["ExternalId"]
    assert (external["MinLength"], external["MaxLength"]) == (2, 128)
    assert external["AllowedPattern"] and external["NoEcho"] is True
    assert params["RoleName"]["Default"] == "LaunchpadWorkspaceRole"


def test_trust_is_the_hub_role_plus_the_external_id(role: dict[str, Any]):
    """Both halves, and nothing else: the ARN alone would let any principal in
    that account assume the role, and an unconditioned ExternalId is not a
    condition at all."""
    trust = role["AssumeRolePolicyDocument"]["Statement"]
    assert len(trust) == 1
    statement = trust[0]
    assert statement["Effect"] == "Allow"
    assert statement["Action"] == "sts:AssumeRole"
    assert statement["Principal"] == {"AWS": {"Ref": "HubRoleArn"}}
    assert statement["Condition"] == {"StringEquals": {"sts:ExternalId": {"Ref": "ExternalId"}}}


def test_session_duration_is_not_longer_than_role_chaining_allows(role: dict[str, Any]):
    """The hub is itself an assumed role, so its chained session is capped at one
    hour whatever this says — and it survives long jobs by refreshing."""
    assert role["MaxSessionDuration"] == 3600


def test_every_statement_lists_sorted_unique_actions(statements: list[dict[str, Any]]):
    """Mechanical, but it is how a permission review stays reviewable: a
    duplicate means two people added the same action, and an out-of-order one
    hides whether a neighbour is present."""
    for statement in statements:
        actions = statement["Action"]
        assert isinstance(actions, list), f"{statement['Sid']}: write Action as a list"
        assert actions == sorted(set(actions)), f"{statement['Sid']}: sort and dedupe Action"


def test_scopable_statements_are_scoped(statements: list[dict[str, Any]]):
    by_sid = {statement["Sid"]: statement for statement in statements}
    assert SCOPED_SIDS <= set(by_sid), "a scoped statement was renamed or removed"
    for sid in SCOPED_SIDS:
        resources = by_sid[sid]["Resource"]
        for resource in resources if isinstance(resources, list) else [resources]:
            assert resource != "*", f"{sid} must name ARNs, not every resource in the account"
            # Fn::Sub keeps the ARN a template expression; read the pattern out of it
            arn = resource["Fn::Sub"] if isinstance(resource, dict) else resource
            assert arn.startswith("arn:"), f"{sid}: {arn} is not an ARN"


def test_pass_role_names_the_services_it_may_pass_to(statements: list[dict[str, Any]]):
    """Without the condition this is "hand any launchpad-* role to anything",
    which is the whole privilege-escalation path a PassRole grant opens."""
    pass_role = next(s for s in statements if s["Sid"] == "PassLaunchpadRoles")
    assert pass_role["Action"] == ["iam:PassRole"]
    assert pass_role["Condition"] == {
        "StringEquals": {
            "iam:PassedToService": [
                "bedrock-agentcore.amazonaws.com",
                "codebuild.amazonaws.com",
            ]
        }
    }


def test_no_managed_knowledge_base_grant_survives(statements: list[dict[str, Any]]):
    """The CN build's Knowledge Bases are self-hosted over OpenSearch (N6): no
    statement may still grant the AWS-managed-Bedrock-KB control/data plane
    (`bedrock:*KnowledgeBase*`, `bedrock:Retrieve`, `bedrock:*DataSource*`), and
    the spoke role may no longer pass a launchpad-* role to `bedrock.amazonaws.com`."""
    text = str(statements)
    assert "knowledge-base/" not in text
    assert "bedrock:Retrieve" not in text
    assert "bedrock:CreateKnowledgeBase" not in text
    assert "bedrock.amazonaws.com" not in text
    for statement in statements:
        for action in statement["Action"]:
            assert not action.startswith("bedrock:"), f"unexpected managed-KB action: {action}"


def test_service_linked_roles_are_limited_to_an_allowlist(statements: list[dict[str, Any]]):
    slr = next(s for s in statements if s["Sid"] == "CreateServiceLinkedRoles")
    services = slr["Condition"]["StringLike"]["iam:AWSServiceName"]
    assert "bedrock-agentcore.amazonaws.com" in services
    # the Registry is local in the CN build (N4)
    assert "agent-registry.amazonaws.com" not in services
    assert all(name.endswith(".amazonaws.com") for name in services)


def test_the_role_cannot_push_images(statements: list[dict[str, Any]]):
    """CodeBuild builds and pushes, under a role of its own that this stack lets
    the hub create. The hub itself only reads back the digest and scan findings,
    so an ECR write grant here would be an unexplained widening."""
    ecr = [
        action
        for statement in statements
        for action in statement["Action"]
        if action.startswith("ecr:")
    ]
    assert set(ecr) == {
        "ecr:CreateRepository",
        "ecr:DescribeImageScanFindings",
        "ecr:DescribeImages",
        "ecr:DescribeRepositories",
    }


def test_only_the_preview_services_are_action_wildcarded(statements: list[dict[str, Any]]):
    """`bedrock-agentcore:*` is argued for in the template: every `List*`/`Create*`
    in that preview API is account-scoped, so an ARN list would grant nothing while
    breaking on the next API. Every other service's actions are enumerated from the
    hub's own call sites — a spoke account holds resources that are none of
    Launchpad's business (a production Cognito pool, for one), so a new
    `<service>:*` has to be argued for here first."""
    wildcards = {
        action
        for statement in statements
        for action in statement["Action"]
        if action.endswith(":*")
    }
    assert wildcards == {"bedrock-agentcore:*"}


def test_no_agent_registry_grant_survives(statements: list[dict[str, Any]]):
    """The CN build's Registry is the local ledger (N4): no action, service principal or
    Sid may still name the AWS Agent Registry."""
    assert "agent-registry" not in str(statements)


def test_the_hub_can_prove_which_account_it_landed_in(statements: list[dict[str, Any]]):
    """validate-access's first call. The grant is declaratory — GetCallerIdentity
    needs no permission — so what this pins is that STS stays that one action and
    does not grow into `sts:*` (which would include AssumeRole onwards)."""
    sts = next(s for s in statements if s["Sid"] == "Sts")
    assert sts["Action"] == ["sts:GetCallerIdentity"]


def test_the_role_arn_is_an_output(template: dict[str, Any]):
    """It is what the operator pastes back into the console."""
    assert template["Outputs"]["RoleArn"]["Value"] == {"Fn::GetAtt": ["WorkspaceRole", "Arn"]}


def test_the_evaluation_worker_runs_on_the_existing_grants(statements: list[dict[str, Any]]):
    """N12 (D14): through this role the hub dispatches evaluation jobs to the account's
    `launchpad-eval-worker*` Runtime (`InvokeAgentRuntime` / `StopRuntimeSession`) and reads
    the worker's artefacts under `evaluations/*` + the dataset snapshots under
    `eval-datasets/*` of the workspace artifacts bucket (`launchpad-artifacts-<account>-
    <region>`). Both ride on statements that already exist (`bedrock-agentcore:*` is
    argued for above; the bucket matches `launchpad-*`), so no new statement is added —
    this pins the coverage so a later narrowing fails here, not in a spoke account."""
    by_sid = {statement["Sid"]: statement for statement in statements}
    agentcore = by_sid["AgentCore"]
    assert "bedrock-agentcore:*" in agentcore["Action"] and agentcore["Resource"] == "*"
    s3 = by_sid["S3Launchpad"]
    assert {"s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"} <= set(
        s3["Action"]
    )
    patterns = [resource["Fn::Sub"] for resource in s3["Resource"]]
    assert any(pattern.endswith(":s3:::launchpad-*") for pattern in patterns)
    assert any(pattern.endswith(":s3:::launchpad-*/*") for pattern in patterns)
