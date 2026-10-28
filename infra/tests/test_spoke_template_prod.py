"""The receive-only prod spoke template (`infra/spoke/launchpad-workspace-role-prod.yaml`).

It is the standard template minus the build path, so the assertions are diffs against the
standard template rather than a second copy of its invariants: same trust and parameters,
strictly fewer permissions, and the specific things that were removed really are gone.
"""

from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk import cloudformation_include as cfn_include
from aws_cdk.assertions import Template

SPOKE = Path(__file__).resolve().parents[1] / "spoke"


def _load(name: str) -> dict[str, Any]:
    app = cdk.App()
    stack = cdk.Stack(app, "spoke")
    cfn_include.CfnInclude(stack, "SpokeRole", template_file=str(SPOKE / name))
    return Template.from_stack(stack).to_json()


def _role(template: dict[str, Any]) -> dict[str, Any]:
    roles = [r for r in template["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 1
    return roles[0]["Properties"]


@pytest.fixture(scope="module")
def standard() -> dict[str, Any]:
    return _load("launchpad-workspace-role.yaml")


@pytest.fixture(scope="module")
def prod() -> dict[str, Any]:
    return _load("launchpad-workspace-role-prod.yaml")


def _statements(template: dict[str, Any]) -> list[dict[str, Any]]:
    """The role's inline policy plus its attached customer managed policy."""
    statements = list(_role(template)["Policies"][0]["PolicyDocument"]["Statement"])
    for resource in template["Resources"].values():
        if resource["Type"] == "AWS::IAM::ManagedPolicy":
            statements += resource["Properties"]["PolicyDocument"]["Statement"]
    return statements


def _actions(template: dict[str, Any]) -> set[str]:
    return {a for s in _statements(template) for a in s["Action"]}


def test_trust_and_session_are_identical_to_the_standard_template(standard, prod):
    std, prd = _role(standard), _role(prod)
    assert prd["AssumeRolePolicyDocument"] == std["AssumeRolePolicyDocument"]
    assert prd["MaxSessionDuration"] == std["MaxSessionDuration"] == 3600


def test_prod_grants_a_strict_subset_of_the_standard_actions(standard, prod):
    extra = _actions(prod) - _actions(standard)
    assert not extra, f"prod must not grant anything the standard template does not: {extra}"


def test_there_is_no_build_permission_at_all(prod):
    assert not {a for a in _actions(prod) if a.startswith("codebuild:")}
    pass_role = next(s for s in _statements(prod) if s["Sid"] == "PassLaunchpadRoles")
    services = pass_role["Condition"]["StringEquals"]["iam:PassedToService"]
    assert "codebuild.amazonaws.com" not in services
    assert "bedrock-agentcore.amazonaws.com" in services


def test_ecr_is_receive_only_and_one_repository(prod):
    ecr = {a for a in _actions(prod) if a.startswith("ecr:")}
    assert "ecr:PutImage" in ecr and "ecr:UploadLayerPart" in ecr
    # reading layers OUT of this account is the source half of a copy
    assert not ecr & {
        "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:GetAuthorizationToken",
    }
    for statement in _statements(prod):
        if not statement["Action"][0].startswith("ecr:"):
            continue
        resource = statement["Resource"]
        if statement["Sid"] == "EcrDescribeRepositoriesForAccessProbe":
            assert statement["Action"] == ["ecr:DescribeRepositories"]
            continue
        assert resource != "*"
        assert resource["Fn::Sub"].endswith("repository/launchpad-agents"), statement["Sid"]
    assert "EcrDescribeImagesForByoc" not in {s["Sid"] for s in _statements(prod)}


def test_released_artifacts_cannot_be_deleted_by_the_hub(standard, prod):
    assert "s3:DeleteObject" in _actions(standard)
    assert "s3:DeleteObject" not in _actions(prod)


def test_deploy_invoke_observe_and_evaluate_survive(prod):
    actions = _actions(prod)
    assert {"bedrock-agentcore:*", "agent-registry:*"} <= actions  # deploy, invoke, evaluate
    assert {"logs:StartQuery", "cloudwatch:GetMetricData"} <= actions  # observe
    assert {"iam:CreateRole", "iam:PassRole", "sts:GetCallerIdentity"} <= actions


def test_project_boundary_statements_match_the_standard_template(standard, prod):
    """Isolated projects (projects design §10.3) work the same in a prod workspace: the
    boundary grants and the CreateRole Deny are copied verbatim."""
    sids = ("IamProjectBoundaryPolicies", "IamProjectBoundaryOnAgentRoles",
            "IamIsolatedAgentRolesNeedABoundary")
    std = {s["Sid"]: s for s in _statements(standard)}
    prd = {s["Sid"]: s for s in _statements(prod)}
    for sid in sids:
        assert prd[sid] == std[sid], sid


def test_cost_tags_work_the_same_in_a_prod_workspace(standard, prod):
    """Cost tags (cost-tags design §1.5): the log-group tagging and Cost Explorer
    statements are copied verbatim; the shared repository can be tagged too."""
    std = {s["Sid"]: s for s in _statements(standard)}
    prd = {s["Sid"]: s for s in _statements(prod)}
    for sid in ("CostTagsAgentLogGroups", "CostExplorerByTag"):
        assert prd[sid] == std[sid], sid
    assert {"ecr:TagResource", "ecr:ListTagsForResource"} <= set(prd["EcrLaunchpad"]["Action"])
