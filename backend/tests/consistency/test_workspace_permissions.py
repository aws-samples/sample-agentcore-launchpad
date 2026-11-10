"""The permission contract between the hub's EC2 role and the workspace bootstrap stages
(N17-obs117; testing.md §6.10).

Two sources that must agree, read independently and compared — nothing copied from either:

* ``app/services/workspace_permissions.STAGE_NEEDS`` — the IAM actions each
  ``workspace_bootstrap.STAGES`` entry issues, with the resource the SERVICE authorises the call
  against (the workspace's region / account as placeholders);
* ``infra/ec2/launchpad-cn-ec2.yaml`` — the instance role's identity policy, rendered for a
  (hub region, account, parameters) pair by ``iam_policy_eval`` (``!Sub`` / ``!If`` / conditions
  resolved, ``AdministratorAccess`` behind ``HubAdminAccess``).

For a workspace region the evaluator answers every need the way IAM would. A need the policy
cannot grant is a FINDING: N17-obs112 (the region-pinned statements — today's live failure,
pinned strict under the default parameters). Two more were found here and fixed on cn/r17-v:
N17-obs130 (the `validate-access` probes listed without a resource, so the resource-scoped
policy could not grant them in ANY region — the probes now ask by name) and N17-obs131 (the
trust-policy refresh of an adopted gateway / CodeBuild role — `IamAdoptAgentRoles` now names
both); the hub-region tests below assert the empty denial set, conditional calls included.
The hermetic twin of the live `validate-access` stage; the live one is the release smoke
``backend/scripts/e2e_workspace_region_smoke.py`` (deploy-ops, not the gate).
"""

from __future__ import annotations

import pytest

from app.services import workspace_bootstrap as wb
from app.services import workspace_permissions as wp
from tests.consistency import iam_policy_eval as ev

HUB_REGION = "cn-northwest-1"
OTHER_REGION = "cn-north-1"
ACCOUNT = "123456789012"
PARTITION = "aws-cn"

TEMPLATE = ev.load_template()

#: botocore client name → IAM action prefix, for the differential against `_PROBES`
_IAM_PREFIX = {"bedrock-agentcore-control": "bedrock-agentcore"}
#: a boto3 operation whose IAM action is spelled differently
_IAM_ACTION = {("s3", "ListBuckets"): "ListAllMyBuckets"}


def _statements(*, hub_admin: bool = False, hub_region: str = HUB_REGION) -> list[ev.Statement]:
    scope = ev.template_scope(
        TEMPLATE, region=hub_region, account=ACCOUNT, partition=PARTITION,
        parameters={"HubAdminAccess": "true" if hub_admin else "false"},
    )
    return ev.instance_role_statements(TEMPLATE, scope)


def _verdicts(ws_region: str, *, hub_admin: bool = False, **filters: bool
              ) -> list[tuple[wp.Need, ev.Verdict]]:
    statements = _statements(hub_admin=hub_admin)
    out = []
    for need in wp.needs(**filters):
        resource = need.render(partition=PARTITION, region=ws_region, account=ACCOUNT)
        out.append((need, ev.evaluate(statements, need.key, resource,
                                      resource_tags=dict(need.tags))))
    return out


def _denied(ws_region: str, **kw: bool) -> list[str]:
    return sorted(
        f"{need.stage} · {need.key} on "
        f"{need.render(partition=PARTITION, region=ws_region, account=ACCOUNT)}"
        for need, verdict in _verdicts(ws_region, **kw) if not verdict.allowed
    )


def _denied_keys(ws_region: str, **kw: bool) -> set[tuple[str, str]]:
    return {
        (need.stage, need.key)
        for need, verdict in _verdicts(ws_region, **kw) if not verdict.allowed
    }


#: the three wildcard-resource shapes N17-obs130 named — must never return to the manifest
UNFILTERED_PROBE_SHAPES = {
    ("validate-access", "ecr:DescribeRepositories", "repository/*"),
    ("validate-access", "codebuild:ListProjects", "*"),
    ("validate-access", "iam:ListRoles", "*"),
}


# ---------------------------------------------------------------------------
# the manifest itself
# ---------------------------------------------------------------------------


def test_the_manifest_names_every_stage_and_only_partition_relative_resources():
    assert list(wp.STAGE_NEEDS) == wb.STAGE_ORDER
    for need in wp.needs(include_implicit=True):
        assert need.resource == "*" or need.resource.startswith("arn:{partition}:"), need
        rendered = need.render(partition="aws-cn", region="r", account="a")
        assert "{" not in rendered and "}" not in rendered, need  # only the three placeholders
        assert need.service == need.service.lower() and need.action[0].isupper(), need
    # every non-ledger stage declares at least one need; finalize is ledger-only
    for stage, entries in wp.STAGE_NEEDS.items():
        assert bool(entries) == (stage != "finalize"), stage


def test_the_manifest_covers_every_probe_the_validate_access_stage_issues():
    """Differential against the stage's own `_PROBES` table: every (client, operation) the
    stage calls is a need of the `validate-access` stage, under its IAM spelling."""
    declared = {(n.service, n.action) for n in wp.STAGE_NEEDS["validate-access"]}
    for client_name, operation, _call in wb._PROBES:
        service = _IAM_PREFIX.get(client_name, client_name)
        action = _IAM_ACTION.get((client_name, operation), operation)
        assert (service, action) in declared, (client_name, operation)
    # and the identity call the stage opens with
    assert any(n.key == "sts:GetCallerIdentity" and n.implicit
               for n in wp.STAGE_NEEDS["validate-access"])


# ---------------------------------------------------------------------------
# the evaluator (hand-built statements and the template's intrinsic functions)
# ---------------------------------------------------------------------------


def test_evaluator_matches_iam_globs_and_lets_an_explicit_deny_win():
    allow = ev.Statement("A", "Allow", ("ecr:*",), ("arn:aws-cn:ecr:r:a:repository/launchpad-*",))
    star = ev.Statement("S", "Allow", ("codebuild:ListProjects",), ("*",))
    deny = ev.Statement("D", "Deny", ("ecr:DeleteRepository",), ("*",))
    statements = [allow, star, deny]
    assert ev.evaluate(statements, "ecr:DescribeRepositories",
                       "arn:aws-cn:ecr:r:a:repository/launchpad-agents").by == "A"
    # a call without a resource is authorised against the wildcard resource, which a
    # resource-scoped statement does not match (the N17-obs112 live shape; N17-obs130 moved
    # the probes off it)
    assert not ev.evaluate(statements, "ecr:DescribeRepositories",
                           "arn:aws-cn:ecr:r:a:repository/*").allowed
    assert ev.evaluate(statements, "codebuild:ListProjects", "*").by == "S"
    assert not ev.evaluate(statements, "codebuild:BatchGetProjects", "*").allowed
    assert ev.evaluate(statements, "ECR:describerepositories",  # actions are case-insensitive
                       "arn:aws-cn:ecr:r:a:repository/launchpad-agents").allowed
    assert not ev.evaluate(statements, "ecr:DescribeRepositories",  # resources are not
                           "arn:aws-cn:ecr:r:a:repository/Launchpad-agents").allowed
    verdict = ev.evaluate(statements, "ecr:DeleteRepository",
                          "arn:aws-cn:ecr:r:a:repository/launchpad-agents")
    assert (verdict.allowed, verdict.by) == (False, "D")
    assert ev.evaluate([], "s3:GetObject", "*").text == "DENIED (no statement matches)"


def test_evaluator_renders_sub_if_and_no_value_the_way_cloudformation_does():
    template = {
        "Parameters": {"Flag": {"Default": "false"}, "Name": {"Default": "x"}},
        "Conditions": {"On": {"Fn::Equals": [{"Ref": "Flag"}, "true"]},
                       "Off": {"Fn::Not": [{"Condition": "On"}]}},
    }
    scope = ev.template_scope(template, region="cn-north-1", account="1" * 12,
                              partition="aws-cn")
    assert ev.render({"Fn::Sub": "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:"
                                 "parameter/${Name}"}, scope) == (
        "arn:aws-cn:ssm:cn-north-1:111111111111:parameter/x")
    assert ev.render({"Fn::Sub": ["${A}-${!B}", {"A": {"Ref": "Name"}}]}, scope) == "x-${B}"
    assert ev.render([{"Fn::If": ["On", "yes", {"Ref": "AWS::NoValue"}]}, "kept"], scope) == [
        "kept"]
    assert ev.render({"Fn::If": ["Off", "yes", "no"]}, scope) == "yes"
    on = ev.template_scope(template, region="cn-north-1", account="1" * 12, partition="aws-cn",
                           parameters={"Flag": "true"})
    assert ev.render({"Fn::If": ["On", "yes", "no"]}, on) == "yes"
    with pytest.raises(KeyError):
        ev.template_scope(template, region="r", account="a", partition="p",
                          parameters={"NotAParameter": "1"})


def test_the_template_renders_to_the_scoped_policy_plus_admin_only_when_asked():
    scoped = _statements()
    assert ev.ADMIN_SID not in {s.sid for s in scoped}
    sids = {s.sid for s in scoped}
    assert {"IamForLaunchpadRoles", "EcrForLaunchpadRepos", "CodeBuildForLaunchpadProjects",
            "AgentCoreControlPlane", "ReadLaunchpadSecretParameters", ev.SSM_CORE_SID} <= sids
    # the conditional SSM ARNs (git deploy key, SiliconFlow) drop out under the defaults
    [ssm] = [s for s in scoped if s.sid == "ReadLaunchpadSecretParameters"]
    assert len(ssm.resources) == 3 and all(":parameter/" in r for r in ssm.resources)
    admin = _statements(hub_admin=True)
    assert ev.ADMIN_SID in {s.sid for s in admin}
    assert len(admin) == len(scoped) + 1


# ---------------------------------------------------------------------------
# the contract — hub region
# ---------------------------------------------------------------------------


def test_hub_region_least_privilege_grants_every_resource_scoped_need():
    """What `make bootstrap` relies on, re-derived for the staged job: in the stack's own
    region the scoped policy covers EVERY unconditional need — the `validate-access` probes
    included since N17-obs130 moved them onto the by-name shapes (was: the three wildcard-
    resource probes denied, pinned strict)."""
    assert _denied(HUB_REGION, include_conditional=False) == []


def test_the_by_name_probes_are_granted_by_the_scoped_statements_not_by_admin():
    """N17-obs130's fix, named: each probe of the ECR / CodeBuild / IAM trio is allowed in the
    hub region by the resource-scoped statement for its service, and the unfiltered shapes the
    finding recorded are still denied — the fix is the call shape, not a wider policy."""
    statements = _statements()
    probes = {(n.key, n.render(partition=PARTITION, region=HUB_REGION, account=ACCOUNT))
              for n in wp.STAGE_NEEDS["validate-access"] if "_probe" in n.source}
    by_statement = {
        key: ev.evaluate(statements, key, resource).by for key, resource in probes
    }
    assert by_statement["ecr:DescribeRepositories"] == "EcrForLaunchpadRepos"
    assert by_statement["codebuild:BatchGetProjects"] == "CodeBuildForLaunchpadProjects"
    assert by_statement["iam:GetRole"] == "IamForLaunchpadRoles"
    assert by_statement["bedrock-agentcore:ListGateways"] == "AgentCoreControlPlane"
    assert by_statement["s3:ListAllMyBuckets"] == "CdkAndStackLifecycle"
    for _stage, key, suffix in UNFILTERED_PROBE_SHAPES:
        resource = (suffix if suffix == "*"
                    else f"arn:{PARTITION}:ecr:{HUB_REGION}:{ACCOUNT}:{suffix}")
        assert not ev.evaluate(statements, key, resource).allowed, (key, resource)
    # and the manifest no longer declares any of them: no wildcard ECR resource, no list action
    declared = {(n.key, n.resource.rsplit(":", 1)[-1])
                for n in wp.STAGE_NEEDS["validate-access"]}
    assert not declared & {(k, r) for _s, k, r in UNFILTERED_PROBE_SHAPES}, declared
    assert ("ecr:DescribeRepositories", "repository/launchpad-agents*") in declared


def test_hub_region_every_conditional_call_is_grantable_too():
    """Conditional needs included (the trust-policy refresh of an adopted role, the first-run
    creates, the hub-colocated memory AssumeRole): nothing the staged job can issue in the
    stack's own region is denied — N17-obs131 (the gateway / CodeBuild role refresh) flipped
    when `IamAdoptAgentRoles` gained the two regional role names."""
    assert _denied(HUB_REGION) == []


WORKSPACE_TAGS = {"launchpad:workspace": "hr-spoke", "launchpad:managed": "true"}
#: what the hub's CDK stack puts on its own roles (infra/stacks/base_stack.py) — no workspace tag
CDK_TWIN_TAGS = {"launchpad:managed": "true"}


def _role_arn(role: str) -> str:
    return f"arn:aws-cn:iam::{ACCOUNT}:role/{role}"


def test_the_trust_policy_refresh_is_granted_for_every_role_the_bootstrap_adopts():
    """N17-obs131's fix, named: `iam:UpdateAssumeRolePolicy` on each of the four roles
    `workspace_iam.ensure_role` creates and re-adopts is allowed by `IamAdoptWorkspaceRoles` —
    in both regions, with and without the EnvName suffix, FOR A ROLE CARRYING THE
    `launchpad:workspace` TAG (N17-obs201: the manifest declares the tags `CreateRole` set) —
    while the broad role statement still lacks the action and the roles the bootstrap never
    adopts stay denied."""
    statements = _statements()
    refreshes = [n for n in wp.needs() if n.key == "iam:UpdateAssumeRolePolicy"]
    assert {n.stage for n in refreshes} == {"iam", "codebuild", "skill-lab"}
    assert len(refreshes) == 4  # execution + gateway (iam stage), codebuild, skill-lab worker
    assert all(dict(n.tags).get("launchpad:workspace") for n in refreshes), "tags declared"
    for region in (HUB_REGION, OTHER_REGION):
        for need in refreshes:
            verdict = ev.evaluate(statements, need.key,
                                  need.render(partition=PARTITION, region=region, account=ACCOUNT),
                                  resource_tags=dict(need.tags))
            assert verdict.allowed and verdict.by == "IamAdoptWorkspaceRoles", (need.label, region)
        for role in (f"launchpad-gateway-role-{region}", f"launchpad-gateway-role-{region}-t1",
                     f"launchpad-codebuild-role-{region}", f"launchpad-codebuild-role-{region}-t1",
                     f"launchpad-agent-execution-role-{region}",
                     f"launchpad-skill-lab-worker-{region}-t1"):
            assert ev.evaluate(statements, "iam:UpdateAssumeRolePolicy", _role_arn(role),
                               resource_tags=WORKSPACE_TAGS).allowed, role
    [broad] = [s for s in statements if s.sid == "IamForLaunchpadRoles"]
    assert "iam:updateassumerolepolicy" not in {a.lower() for a in broad.actions}
    for role in ("launchpad-memory-admin-cn-northwest-1", "launchpad-cn-ops-cn-northwest-1",
                 "launchpad-workspace-role"):
        assert not ev.evaluate(statements, "iam:UpdateAssumeRolePolicy", _role_arn(role),
                               resource_tags=WORKSPACE_TAGS).allowed, role


def test_N17_obs201_the_hubs_cdk_made_twins_are_denied_the_trust_refresh_by_iam_itself():
    """The CDK stack's `launchpad-gateway-role-<region>` / `launchpad-agent-execution-role-
    <region>` carry `launchpad:managed` only: the name patterns match, the `launchpad:workspace`
    Condition does not — DENIED by IAM, no longer only by `workspace_iam.ensure_role`'s tag
    check. The same names tagged by the bootstrap are allowed; an untagged role (no tags at all)
    is denied; the tag's VALUE is irrelevant (`StringLike "*"`)."""
    statements = _statements()
    for role in ("launchpad-gateway-role-cn-northwest-1",
                 "launchpad-agent-execution-role-cn-northwest-1",
                 "launchpad-codebuild-role-cn-northwest-1",
                 "launchpad-skill-lab-worker-cn-northwest-1"):
        twin = ev.evaluate(statements, "iam:UpdateAssumeRolePolicy", _role_arn(role),
                           resource_tags=CDK_TWIN_TAGS)
        assert not twin.allowed and twin.by is None, (role, twin.text)
        bare = ev.evaluate(statements, "iam:UpdateAssumeRolePolicy", _role_arn(role))
        assert not bare.allowed, (role, bare.text)
        ours = ev.evaluate(statements, "iam:UpdateAssumeRolePolicy", _role_arn(role),
                           resource_tags={"launchpad:workspace": "any-value-at-all"})
        assert ours.allowed and ours.by == "IamAdoptWorkspaceRoles", (role, ours.text)
    # the other actions of the adopt path (GetRole, TagRole, PutRolePolicy, …) are the broad
    # statement's and stay unconditional — the Condition narrows the trust WRITE only
    for action in ("iam:GetRole", "iam:TagRole", "iam:PutRolePolicy", "iam:ListRoleTags"):
        gateway = _role_arn("launchpad-gateway-role-cn-northwest-1")
        verdict = ev.evaluate(statements, action, gateway)
        assert verdict.allowed and verdict.by == "IamForLaunchpadRoles", (action, verdict.text)


def test_N17_obs201_the_per_agent_roles_need_their_own_tag():
    """`agent_iam.ensure_role` tags `launchpad-agent-<name>-<id8>[-env]` with
    `launchpad:agent-id` in its CreateRole call; `IamAdoptAgentRoles` keeps the per-agent
    pattern under THAT tag's Condition, so the CDK execution role — which the `launchpad-agent-*`
    glob also matches — is denied through this statement too."""
    statements = _statements()
    agent_role = _role_arn("launchpad-agent-hr-helpdesk-0123abcd-t1")
    agent_tags = {"launchpad:agent-id": "ag_0123abcd", "launchpad:managed": "true"}
    ours = ev.evaluate(statements, "iam:UpdateAssumeRolePolicy", agent_role,
                       resource_tags=agent_tags)
    assert ours.allowed and ours.by == "IamAdoptAgentRoles", ours.text
    assert not ev.evaluate(statements, "iam:UpdateAssumeRolePolicy", agent_role).allowed
    assert not ev.evaluate(statements, "iam:UpdateAssumeRolePolicy", agent_role,
                           resource_tags=CDK_TWIN_TAGS).allowed
    # a workspace-tagged role under the per-agent pattern is NOT the per-agent statement's
    assert not ev.evaluate(statements, "iam:UpdateAssumeRolePolicy", agent_role,
                           resource_tags=WORKSPACE_TAGS).allowed
    cdk_exec = _role_arn("launchpad-agent-execution-role-cn-northwest-1")
    assert not ev.evaluate(statements, "iam:UpdateAssumeRolePolicy", cdk_exec,
                           resource_tags=CDK_TWIN_TAGS).allowed


def test_evaluator_models_a_resource_tag_condition_and_refuses_the_rest():
    """N17-obs201: `StringLike` / `StringEquals` on `iam:ResourceTag/<key>` — ANDed within a
    statement, a missing key is false, globs are case-sensitive; any other operator or key is a
    `NotImplementedError`, never a silent allow."""
    like = ev.Condition("StringLike", "iam:ResourceTag/launchpad:workspace", ("*",))
    equals = ev.Condition("StringEquals", "iam:ResourceTag/launchpad:managed", ("true",))
    s = ev.Statement("S", "Allow", ("iam:UpdateAssumeRolePolicy",), ("arn:aws-cn:iam::1:role/x",),
                     (like, equals))
    arn = "arn:aws-cn:iam::1:role/x"
    both = {"launchpad:workspace": "w", "launchpad:managed": "true"}
    assert ev.evaluate([s], "iam:UpdateAssumeRolePolicy", arn, resource_tags=both).by == "S"
    assert not ev.evaluate([s], "iam:UpdateAssumeRolePolicy", arn,
                           resource_tags={"launchpad:workspace": "w"}).allowed  # ANDed
    assert not ev.evaluate([s], "iam:UpdateAssumeRolePolicy", arn,
                           resource_tags={"launchpad:managed": "true"}).allowed  # missing key
    assert not ev.evaluate([s], "iam:UpdateAssumeRolePolicy", arn,
                           resource_tags={"launchpad:workspace": "w", "launchpad:managed": "True"}
                           ).allowed  # case-sensitive
    assert not ev.evaluate([s], "iam:UpdateAssumeRolePolicy", arn).allowed
    prefix = ev.Condition("StringLike", "iam:ResourceTag/env", ("t?",))
    assert ev.condition_holds(prefix, {"env": "t1"})
    assert not ev.condition_holds(prefix, {"env": "t10"})
    with pytest.raises(NotImplementedError):
        ev._conditions({"ArnLike": {"aws:SourceArn": "x"}})
    with pytest.raises(NotImplementedError):
        ev._conditions({"StringLike": {"aws:RequestTag/x": "*"}})
    # the template's own statements parse (the rendered role has exactly the two conditional ones)
    conditional = {s.sid: s.conditions for s in _statements() if s.conditions}
    assert conditional == {
        "IamAdoptAgentRoles": (
            ev.Condition("StringLike", "iam:ResourceTag/launchpad:agent-id", ("*",)),),
        "IamAdoptWorkspaceRoles": (
            ev.Condition("StringLike", "iam:ResourceTag/launchpad:workspace", ("*",)),),
    }


# ---------------------------------------------------------------------------
# the contract — the hub's OTHER region (N17-obs112)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True, raises=AssertionError,
    reason="N17-obs112 — the ops policy pins its ECR, CodeBuild, Secrets Manager and "
           "CloudTrail statements to ${AWS::Region} (the stack's region), so a same-account "
           "workspace in the hub's other region is denied at validate-access "
           "(ecr DescribeRepositories — the live 2026-10-20 failure) and would be denied again "
           "at model-credential (Identity's secret), storage and codebuild; ruled onto "
           "HubAdminAccess=true for the default host, t1 stays least-privilege",
)
def test_N17_obs112_other_region_bootstrap_is_grantable_under_the_default_parameters():
    assert _denied(OTHER_REGION, include_degrading=False, include_conditional=False) == []


def test_other_region_denials_are_the_region_pinned_statements():
    """The N17-obs112 pin's positive twin: EVERY unconditional denial in the other region is a
    statement whose resource ARN carries `${AWS::Region}` — ECR (the by-name probe and the
    storage stage's describe), CodeBuild (the by-name probe, the foreign-resource check and the
    project update) and Secrets Manager (AgentCore Identity's secret, authorised against the
    caller — N11-bug7). Names them so the register row and the operator read the same list."""
    denied = _denied_keys(OTHER_REGION, include_degrading=False, include_conditional=False)
    assert denied == {
        ("validate-access", "ecr:DescribeRepositories"),
        ("validate-access", "codebuild:BatchGetProjects"),
        ("model-credential", "secretsmanager:CreateSecret"),
        ("model-credential", "secretsmanager:PutSecretValue"),
        ("model-credential", "secretsmanager:UpdateSecret"),
        ("model-credential", "secretsmanager:DescribeSecret"),
        ("model-credential", "secretsmanager:TagResource"),
        ("storage", "ecr:DescribeRepositories"),
        ("codebuild", "codebuild:BatchGetProjects"),
        ("codebuild", "codebuild:UpdateProject"),
    }
    # the first probe IAM refuses is the one the live job log showed (ecr before codebuild)
    first = next(
        (need, v) for need, v in _verdicts(OTHER_REGION, include_implicit=False)
        if need.stage == "validate-access" and not v.allowed
    )
    assert first[0].key == "ecr:DescribeRepositories"
    assert first[0].render(partition=PARTITION, region=OTHER_REGION, account=ACCOUNT).endswith(
        f":ecr:{OTHER_REGION}:{ACCOUNT}:repository/launchpad-agents*")


def test_other_region_degrading_needs_that_are_denied_are_named_too():
    """The stages that degrade instead of failing (skill-lab's build, the model-credential
    SSM pass, Transaction Search's CloudTrail channel) lose their feature in the other region
    under the scoped policy — listed so the degrade detail an operator will see is expected."""
    unconditional = _denied_keys(OTHER_REGION, include_conditional=False)
    degrading = {
        (n.stage, n.key) for n, v in _verdicts(OTHER_REGION, include_conditional=False)
        if not v.allowed and n.degrades
    }
    assert degrading == {
        ("skill-lab", "codebuild:StartBuild"),
        ("skill-lab", "codebuild:BatchGetBuilds"),
        ("skill-lab", "ecr:DescribeImages"),
    }
    assert degrading <= unconditional


def test_the_ssm_core_policy_covers_the_model_credential_parameter_read_in_both_regions():
    """rev-r17 nit: `AmazonSSMManagedInstanceCore` grants `ssm:GetParameter` / `GetParameters`
    on `*`, so the model-credential stage's registry pass (D21) is allowed even where the
    scoped `ReadLaunchpadSecretParameters` statement is region-pinned — the ONLY SSM need the
    bootstrap has, and the reason no SSM row appears in the other-region denials above."""
    [need] = [n for n in wp.needs() if n.key == "ssm:GetParameter"]
    for region in (HUB_REGION, OTHER_REGION):
        resource = need.render(partition=PARTITION, region=region, account=ACCOUNT)
        verdict = ev.evaluate(_statements(), need.key, resource)
        assert verdict.allowed, (region, verdict.text)
    # in the hub region the scoped statement matches first (listed before the managed policy)
    hub = ev.evaluate(_statements(), need.key,
                      need.render(partition=PARTITION, region=HUB_REGION, account=ACCOUNT))
    assert hub.by == "ReadLaunchpadSecretParameters"
    other = ev.evaluate(_statements(), need.key,
                        need.render(partition=PARTITION, region=OTHER_REGION, account=ACCOUNT))
    assert other.by == ev.SSM_CORE_SID


def test_N17_obs203_the_console_host_may_write_and_delete_provider_key_parameters():
    """The console's own IAM needs (not a bootstrap stage): a model-provider API key typed in
    the console is written / tagged / read back / deleted on `parameter/launchpad-*-api-key` in
    the HUB region. Every need is grantable under the default parameters and the writes are
    granted by the new `SsmWriteLaunchpadSecretParameters` statement — by name, so a renamed or
    dropped statement is caught here and not on the live host (502 `model_provider.
    ssm_write_failed` names the same Sid)."""
    statements = _statements()
    by_sid = {}
    for need in wp.CONSOLE_NEEDS:
        resource = need.render(partition=PARTITION, region=HUB_REGION, account=ACCOUNT)
        verdict = ev.evaluate(statements, need.key, resource)
        assert verdict.allowed, (need.label, verdict.text)
        by_sid[need.action] = verdict.by
    assert by_sid["PutParameter"] == "SsmWriteLaunchpadSecretParameters"
    assert by_sid["DeleteParameter"] == "SsmWriteLaunchpadSecretParameters"
    assert by_sid["AddTagsToResource"] == "SsmWriteLaunchpadSecretParameters"
    assert by_sid["GetParameter"] == "ReadLaunchpadSecretParameters"  # the existing read grant
    # the write grant covers the key family only — the console password parameter stays
    # read-only for the host (its name does not match the pattern)
    password = f"arn:{PARTITION}:ssm:{HUB_REGION}:{ACCOUNT}:parameter/launchpad-console-password"
    assert not ev.evaluate(statements, "ssm:PutParameter", password).allowed
    # and every console need names the same resource shape the bootstrap's SSM read does
    assert {n.resource for n in wp.CONSOLE_NEEDS} == {
        n.resource for n in wp.needs() if n.key == "ssm:GetParameter"}


def test_the_policy_interceptor_stage_is_granted_in_the_hub_region():
    """Stream B3: every call of the hub-only `policy-interceptor` stage is allowed by the
    least-privilege role (HubAdminAccess=false), each by the statement written for it."""
    statements = _statements()
    by = {}
    for need in wp.INTERCEPTOR_NEEDS:
        resource = need.render(partition=PARTITION, region=HUB_REGION, account=ACCOUNT)
        verdict = ev.evaluate(statements, need.key, resource)
        assert verdict.allowed, (need.label, verdict.text)
        by[(need.key, need.resource)] = verdict.by
    lambda_sids = {v for (k, _r), v in by.items() if k.startswith("lambda:")}
    assert lambda_sids == {"LambdaForPolicyInterceptor"}
    assert {v for (k, _r), v in by.items() if k.startswith("ec2:")} == {
        "Ec2DescribeForPolicyInterceptorVpc"}
    # the function grant is not a wildcard over other functions, and never Invoke / Delete
    other = f"arn:{PARTITION}:lambda:{HUB_REGION}:{ACCOUNT}:function:someone-else"
    assert not ev.evaluate(statements, "lambda:UpdateFunctionCode", other).allowed
    own = f"arn:{PARTITION}:lambda:{HUB_REGION}:{ACCOUNT}:function:launchpad-policy-interceptor"
    assert by and not ev.evaluate(statements, "lambda:DeleteFunction", own).allowed


def test_hub_admin_access_grants_every_need_in_both_regions():
    """The N17-obs112 ruling: `HubAdminAccess=true` makes the instance role account-admin, so
    every stage need — probes, conditional calls and degrading features included — is allowed
    for a workspace in any region of the partition. In the hub region the scoped statements
    answer first and the admin policy is never consulted (N17-obs130 / N17-obs131 closed the
    last gaps); in the other region it is what grants the `${AWS::Region}`-pinned needs."""
    for region in (HUB_REGION, OTHER_REGION):
        assert _denied(region, hub_admin=True) == [], region
    hub_by = {v.by for _n, v in _verdicts(HUB_REGION, hub_admin=True) if v.allowed}
    assert ev.ADMIN_SID not in hub_by
    other_by = {v.by for _n, v in _verdicts(OTHER_REGION, hub_admin=True) if v.allowed}
    assert ev.ADMIN_SID in other_by
