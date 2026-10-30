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

import json
from pathlib import Path
from typing import Any

import aws_cdk as cdk
import pytest
from aws_cdk import cloudformation_include as cfn_include
from aws_cdk.assertions import Template

SPOKE = Path(__file__).resolve().parents[1] / "spoke"
TEMPLATE = SPOKE / "launchpad-workspace-role.yaml"

# IAM's quotas, counted on the policy JSON with whitespace removed: all inline
# policies of one role together, and each customer managed policy on its own.
INLINE_POLICIES_PER_ROLE_MAX = 10240
MANAGED_POLICY_MAX = 6144

# Statements whose resources Launchpad's naming discipline makes scopable. The
# rest are `*` by necessity (documented per statement in the template); a bare
# `*` appearing in one of THESE is the mistake worth failing on.
SCOPED_SIDS = frozenset(
    {
        "IamOnLaunchpadRoles",
        "IamProjectBoundaryPolicies",
        "IamProjectBoundaryOnAgentRoles",
        "IamIsolatedAgentRolesNeedABoundary",
        "ReadOwnRoleForTemplateVersion",
        "PassLaunchpadRoles",
        "S3Launchpad",
        "EcrLaunchpad",
        "CodeBuildLaunchpad",
        "BedrockKnowledgeBases",
        "CostTagsAgentLogGroups",
        "TransactionSearchLogGroups",
        "TransactionSearchServiceLinkedRole",
        "TransactionSearchCloudTrailChannel",
        "EvaluationResultLogGroups",
    }
)


def _load(path: Path) -> dict[str, Any]:
    app = cdk.App()
    stack = cdk.Stack(app, "spoke")
    cfn_include.CfnInclude(stack, "SpokeRole", template_file=str(path))
    return Template.from_stack(stack).to_json()


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    """The template as CloudFormation will see it.

    CDK adds its own `BootstrapVersion` parameter and `CheckBootstrapVersion`
    rule to the synthesized stack; the assertions below name what they read, so
    those extras are simply ignored.
    """
    return _load(TEMPLATE)


@pytest.fixture(scope="module")
def role(template: dict[str, Any]) -> dict[str, Any]:
    roles = [
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::IAM::Role"
    ]
    assert len(roles) == 1, "the template provisions exactly one role and nothing else"
    return roles[0]["Properties"]


def _managed_policies(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        logical_id: resource["Properties"]
        for logical_id, resource in template["Resources"].items()
        if resource["Type"] == "AWS::IAM::ManagedPolicy"
    }


@pytest.fixture(scope="module")
def statements(template: dict[str, Any], role: dict[str, Any]) -> list[dict[str, Any]]:
    """Every Allow/Deny the role carries: its one inline policy plus the customer
    managed policy the template attaches (the split is only about IAM's size
    quotas, so the invariants below hold across both)."""
    policies = role["Policies"]
    assert len(policies) == 1
    managed = _managed_policies(template)
    assert role["ManagedPolicyArns"] == [{"Ref": logical_id} for logical_id in managed]
    return policies[0]["PolicyDocument"]["Statement"] + [
        statement
        for policy in managed.values()
        for statement in policy["PolicyDocument"]["Statement"]
    ]


def test_the_stack_creates_the_role_its_managed_policy_and_nothing_else(
    template: dict[str, Any],
):
    kinds = sorted(resource["Type"] for resource in template["Resources"].values())
    assert kinds == ["AWS::IAM::ManagedPolicy", "AWS::IAM::Role", "AWS::IAM::RolePolicy"]
    (policy,) = _managed_policies(template).values()
    # The hub may write customer managed policies only under launchpad-project-*
    # (IamProjectBoundaryPolicies); a name there would let it rewrite its own grants.
    assert policy["ManagedPolicyName"] == {"Fn::Sub": "${RoleName}-bedrock-telemetry"}
    assert "Roles" not in policy, "attach through ManagedPolicyArns so it exists first"


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
                "bedrock.amazonaws.com",
                "bedrock-agentcore.amazonaws.com",
                "codebuild.amazonaws.com",
            ]
        }
    }


def test_service_linked_roles_are_limited_to_an_allowlist(statements: list[dict[str, Any]]):
    slr = next(s for s in statements if s["Sid"] == "CreateServiceLinkedRoles")
    services = slr["Condition"]["StringLike"]["iam:AWSServiceName"]
    assert "bedrock-agentcore.amazonaws.com" in services
    assert "agent-registry.amazonaws.com" in services
    assert all(name.endswith(".amazonaws.com") for name in services)


def test_the_role_pushes_images_only_through_the_artifact_copy_statement(
    statements: list[dict[str, Any]],
):
    """CodeBuild builds and pushes, under a role of its own that this stack lets
    the hub create. The hub itself only reads back the digest and scan findings —
    except for a PROMOTED image (roadmap T24), which it copies by digest through
    the ECR API. That grant lives in one named, single-repository statement so an
    ECR write anywhere else is still an unexplained widening."""
    by_sid = {s["Sid"]: s for s in statements}
    writes = {"ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload", "ecr:PutImage",
              "ecr:UploadLayerPart"}
    for statement in statements:
        pushed = writes & set(statement["Action"])
        assert not pushed or statement["Sid"] == "EcrArtifactCopy", statement["Sid"]
    copy = by_sid["EcrArtifactCopy"]
    assert set(copy["Action"]) >= writes | {"ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"}
    assert copy["Resource"] != "*" and copy["Resource"]["Fn::Sub"].endswith(
        "repository/launchpad-agents"
    )
    assert "ecr:GetAuthorizationToken" not in {
        a for s in statements for a in s["Action"]
    }


def test_only_the_preview_services_are_action_wildcarded(statements: list[dict[str, Any]]):
    """`bedrock-agentcore:*` and `agent-registry:*` are argued for in the template:
    every `List*`/`Create*` in those preview APIs is account-scoped, so an ARN list
    would grant nothing while breaking on the next API. Every other service's
    actions are enumerated from the hub's own call sites — a spoke account holds
    resources that are none of Launchpad's business (a production Cognito pool, for
    one), so a new `<service>:*` has to be argued for here first."""
    wildcards = {
        action
        for statement in statements
        for action in statement["Action"]
        if action.endswith(":*")
    }
    assert wildcards == {"agent-registry:*", "bedrock-agentcore:*"}


def test_the_hub_can_prove_which_account_it_landed_in(statements: list[dict[str, Any]]):
    """validate-access's first call. The grant is declaratory — GetCallerIdentity
    needs no permission — so what this pins is that STS stays that one action and
    does not grow into `sts:*` (which would include AssumeRole onwards)."""
    sts = next(s for s in statements if s["Sid"] == "Sts")
    assert sts["Action"] == ["sts:GetCallerIdentity"]


def test_the_role_arn_is_an_output(template: dict[str, Any]):
    """It is what the operator pastes back into the console."""
    assert template["Outputs"]["RoleArn"]["Value"] == {"Fn::GetAtt": ["WorkspaceRole", "Arn"]}


def test_a_fresh_region_can_turn_on_transaction_search(statements: list[dict[str, Any]]):
    """Found on real AWS: with only xray:UpdateTraceSegmentDestination the call is
    refused in a region that never had Transaction Search, because the service also
    exercises the indexing rule, the aws/spans log group and Application Signals on
    the caller's behalf. Without these a new-region workspace has no aws/spans group
    and every trace, observability view and evaluation there reads nothing."""
    granted = {action for statement in statements for action in statement["Action"]}
    required = {
        "xray:GetTraceSegmentDestination", "xray:UpdateTraceSegmentDestination",
        "xray:GetIndexingRules", "xray:UpdateIndexingRule",
        "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutRetentionPolicy",
        "logs:PutResourcePolicy", "logs:DescribeResourcePolicies",
        "application-signals:StartDiscovery", "iam:GetRole",
        "cloudtrail:CreateServiceLinkedChannel", "cloudtrail:GetChannel",
    }
    assert required <= granted, f"missing {sorted(required - granted)}"
    by_sid = {statement["Sid"]: statement for statement in statements}
    # the log-group writes are the documented two groups, not every group — each
    # as the bare group ARN (CreateLogGroup is checked against it) and its `:*` form
    groups = by_sid["TransactionSearchLogGroups"]["Resource"]
    assert len(groups) == 4 and all(
        "aws/spans" in g["Fn::Sub"] or "application-signals/data" in g["Fn::Sub"] for g in groups)


def test_a_fresh_region_can_run_its_first_batch_evaluation(statements: list[dict[str, Any]]):
    """Found on real AWS: StartBatchEvaluation creates its results log group with the
    caller's credentials, so a region that never ran one needs logs:CreateLogGroup on
    the evaluations prefix — scoped to that prefix, not every group in the account."""
    by_sid = {statement["Sid"]: statement for statement in statements}
    statement = by_sid["EvaluationResultLogGroups"]
    assert {"logs:CreateLogGroup", "logs:CreateLogStream"} <= set(statement["Action"])
    # and reads the per-session results back (run results route, Agent-DLC snapshot)
    assert "logs:GetLogEvents" in statement["Action"]
    (resource,) = statement["Resource"]
    assert resource["Fn::Sub"].endswith(":log-group:/aws/bedrock-agentcore/evaluations/*")


def test_the_knowledge_base_console_can_list(statements: list[dict[str, Any]]):
    """Found on real AWS: the knowledge-base page answered 403 because the role
    could manage a knowledge base but not list them. ListKnowledgeBases has no
    resource type, so it sits with CreateKnowledgeBase on `*`; ListDataSources
    takes the knowledge-base ARN and stays in the scoped statement."""
    by_sid = {statement["Sid"]: statement for statement in statements}
    assert "bedrock:ListKnowledgeBases" in by_sid["BedrockCreateKnowledgeBase"]["Action"]
    assert "bedrock:ListDataSources" in by_sid["BedrockKnowledgeBases"]["Action"]


def test_the_hub_can_call_models_and_guardrails(statements: list[dict[str, Any]]):
    """Found on real AWS: intent clustering, suggestions, prompt optimization and
    the judge test-invoke on CreateEvaluator all failed with bedrock:InvokeModel
    denied, and the PII guardrail preset could neither be created nor applied.
    Invocation is scoped to models and this account's inference profiles; the
    guardrail reads are scoped to this account's guardrails."""
    by_sid = {statement["Sid"]: statement for statement in statements}
    infer = by_sid["BedrockModelInference"]
    assert set(infer["Action"]) == {"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"}
    resources = [r["Fn::Sub"] for r in infer["Resource"]]
    assert all(":foundation-model/" in r or ":${AWS::AccountId}:" in r for r in resources)
    assert any(r.endswith(":inference-profile/*") for r in resources)
    guard = by_sid["BedrockGuardrails"]
    assert set(guard["Action"]) == {"bedrock:ApplyGuardrail", "bedrock:GetGuardrail"}
    assert guard["Resource"]["Fn::Sub"].endswith(":${AWS::AccountId}:guardrail/*")
    create = set(by_sid["BedrockGuardrailsCreate"]["Action"])
    assert create == {"bedrock:CreateGuardrail", "bedrock:ListGuardrails"}


def _sub(value: Any) -> str:
    return value["Fn::Sub"] if isinstance(value, dict) else value


def test_project_boundary_policies_are_scoped_to_their_prefix(statements: list[dict[str, Any]]):
    """Projects design §10.3: the hub writes only `launchpad-project-*` policies — the
    per-(project, workspace) boundaries — and never deletes one outright or edits
    another customer managed policy."""
    by_sid = {statement["Sid"]: statement for statement in statements}
    policies = by_sid["IamProjectBoundaryPolicies"]
    assert policies["Effect"] == "Allow"
    assert {"iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:DeletePolicyVersion"} <= set(
        policies["Action"]
    )
    assert "iam:DeletePolicy" not in policies["Action"]
    assert _sub(policies["Resource"]).endswith(":policy/launchpad-project-*")
    # no other statement may touch managed policies
    managed = {"iam:CreatePolicy", "iam:CreatePolicyVersion", "iam:DeletePolicy",
               "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:TagPolicy"}
    others = {a for s in statements if s["Sid"] != "IamProjectBoundaryPolicies"
              for a in s["Action"] if a in managed}
    assert others == set(), others


def test_a_boundary_can_only_be_a_project_boundary(statements: list[dict[str, Any]]):
    by_sid = {statement["Sid"]: statement for statement in statements}
    put = by_sid["IamProjectBoundaryOnAgentRoles"]
    assert put["Action"] == ["iam:PutRolePermissionsBoundary"]
    assert _sub(put["Resource"]).endswith(":role/launchpad-agent-*")
    condition = put["Condition"]["StringLike"]["iam:PermissionsBoundary"]
    assert _sub(condition).endswith(":policy/launchpad-project-*")
    # removing a boundary is never granted
    assert not any("iam:DeleteRolePermissionsBoundary" in s["Action"] for s in statements)


def test_an_isolated_agent_role_cannot_be_created_without_a_boundary(
    statements: list[dict[str, Any]],
):
    """The Deny pairs the request tag with a negated boundary match: both must hold, so
    shared projects' roles (no isolation tag) are untouched, and an isolated one with
    no boundary at all is refused (a negated operator is true for an absent key)."""
    by_sid = {statement["Sid"]: statement for statement in statements}
    deny = by_sid["IamIsolatedAgentRolesNeedABoundary"]
    assert deny["Effect"] == "Deny" and deny["Action"] == ["iam:CreateRole"]
    assert _sub(deny["Resource"]).endswith(":role/launchpad-agent-*")
    assert deny["Condition"]["StringEquals"] == {
        "aws:RequestTag/launchpad:isolation": "isolated"
    }
    boundary = deny["Condition"]["StringNotLike"]["iam:PermissionsBoundary"]
    assert _sub(boundary).endswith(":policy/launchpad-project-*")


def test_every_cost_tagged_resource_can_be_tagged(statements: list[dict[str, Any]]):
    """Cost tags (cost-tags design §1.2 / §1.5): each resource the platform tags has
    its tag action, on the resources Launchpad owns, plus the Cost Explorer reads."""
    by_sid = {s["Sid"]: s for s in statements}
    expected = {
        "IamOnLaunchpadRoles": {"iam:TagRole", "iam:ListRoleTags"},
        "EcrLaunchpad": {"ecr:TagResource", "ecr:ListTagsForResource"},
        "EcrProjectRepositories": {"ecr:TagResource", "ecr:ListTagsForResource"},
        "CodeBuildLaunchpad": {"codebuild:CreateProject", "codebuild:UpdateProject"},
        "BedrockKnowledgeBases": {"bedrock:TagResource"},
        "SecretsManagerForTokenVault": {"secretsmanager:TagResource"},
        "CostTagsAgentLogGroups": {"logs:TagResource", "logs:ListTagsForResource"},
        "AgentCoreAndRegistry": {"bedrock-agentcore:*"},
        "CostExplorerByTag": {"ce:GetCostAndUsage", "ce:ListCostAllocationTags",
                              "ce:UpdateCostAllocationTagsStatus"},
    }
    for sid, actions in expected.items():
        assert actions <= set(by_sid[sid]["Action"]), sid
    logs = by_sid["CostTagsAgentLogGroups"]["Resource"]
    assert len(logs) == 1 and logs[0]["Fn::Sub"].endswith(
        "log-group:/aws/bedrock-agentcore/runtimes/*")


def _render(value: Any) -> Any:
    """A policy document as IAM receives it, for measuring. Placeholders are sized
    like the real values; a list parameter (the approved subnets / security groups)
    is three ids, and a condition takes its larger branch."""
    if isinstance(value, list):
        return [_render(item) for item in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {"Fn::Sub"}:
        return (value["Fn::Sub"].replace("${AWS::Partition}", "aws")
                .replace("${AWS::AccountId}", "123456789012")
                .replace("${RoleName}", "LaunchpadWorkspaceRole"))
    if set(value) == {"Ref"}:
        assert value["Ref"] in {"ApprovedSubnetIds", "ApprovedSecurityGroupIds"}, value
        return ["subnet-0123456789abcdef0"] * 3
    if set(value) == {"Fn::If"}:
        return _render(value["Fn::If"][1])
    assert not any(key.startswith("Fn::") for key in value), value
    return {key: _render(item) for key, item in value.items()}


def _size(document: dict[str, Any]) -> int:
    return len(json.dumps(_render(document), separators=(",", ":")))


@pytest.mark.parametrize(
    "name", ["launchpad-workspace-role.yaml", "launchpad-workspace-role-prod.yaml"])
def test_the_policies_fit_iams_size_quotas(name: str):
    """Found on real AWS (2026-10-28): the merged permission set rendered to 10291
    characters in one inline policy, so the stack could neither be created nor
    updated (IAM refuses a role whose inline policies exceed 10240 in total), and
    a spoke stuck on the older template answered the knowledge-base page with
    bedrock:ListKnowledgeBases denied. Every inline policy on the role — the
    network-enforcement Deny included — counts toward the one quota."""
    template = _load(SPOKE / name)
    resources = template["Resources"].values()
    inline = [policy["PolicyDocument"]
              for resource in resources if resource["Type"] == "AWS::IAM::Role"
              for policy in resource["Properties"]["Policies"]]
    inline += [resource["Properties"]["PolicyDocument"]
               for resource in resources if resource["Type"] == "AWS::IAM::RolePolicy"]
    total = sum(_size(document) for document in inline)
    assert total <= INLINE_POLICIES_PER_ROLE_MAX, f"inline policies: {total} characters"
    for logical_id, policy in _managed_policies(template).items():
        size = _size(policy["PolicyDocument"])
        assert size <= MANAGED_POLICY_MAX, f"{logical_id}: {size} characters"


@pytest.mark.parametrize(
    "name", ["launchpad-workspace-role.yaml", "launchpad-workspace-role-prod.yaml"])
def test_the_role_can_read_its_own_template_version_and_nothing_more(name: str):
    """The console's "update the spoke stack" warning reads the role's
    launchpad:template-version tag through GetRole on the role itself; the grant
    names only that role, so it reveals nothing about any other role."""
    template = _load(SPOKE / name)
    (role,) = [r["Properties"] for r in template["Resources"].values()
               if r["Type"] == "AWS::IAM::Role"]
    tags = {tag["Key"]: tag["Value"] for tag in role["Tags"]}
    assert tags["launchpad:template-version"].isdigit()
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    read = next(s for s in statements if s["Sid"] == "ReadOwnRoleForTemplateVersion")
    assert read["Action"] == ["iam:GetRole"]
    assert read["Resource"] == {
        "Fn::Sub": "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/${RoleName}"}
