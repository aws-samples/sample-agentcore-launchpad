"""The IAM contract of the workspace bootstrap job, written down (N17-obs117).

`workspace_bootstrap.STAGES` provisions a workspace with the hub's own credentials — the EC2
instance role of `infra/ec2/launchpad-cn-ec2.yaml` for a same-account workspace, the assumed
spoke role for a cross-account one. Which IAM actions each stage issues, and on which resource,
was never declared anywhere a test could read: the backend suite stubs AWS, the template tests
check the policy's shape, and the one live check (`validate-access`) runs only when an operator
registers a workspace. N17-obs112 (2026-10-20) is what that gap costs — a same-account
workspace in the hub's OTHER region failed at `validate-access` with
`ecr DescribeRepositories … AccessDeniedException`, because the ops policy pins its ECR, SSM
and CodeBuild statements to the stack's region.

This module is the declarative half of the fix: one ``Need`` per IAM action a stage issues,
derived by reading each ``_stage_*`` function and the wrappers it calls (the ``source`` field
names them), with the resource expressed the way the SERVICE evaluates it —
``{partition}`` / ``{region}`` / ``{account}`` are the WORKSPACE's values, and an action that
takes no resource-level permission (or that the stage issues without naming a resource) is
``"*"``. ``backend/tests/consistency/test_workspace_permissions.py``
renders the hub template's policy for a (hub region, parameters) pair and checks every need
against it for a workspace region — the hermetic twin of the live `validate-access` stage.

Honesty rules for this table: a need lists what the code calls, not what a tidier policy
would grant; a call the stage issues only under a condition (an adopted role whose trust policy
drifted, the hub-colocated memory stage) says so in ``when``; a stage whose failure DEGRADES the
workspace instead of failing the job (registry defaults, memory, skill-lab, observability,
policy — see each stage's docstring) marks its needs ``degrades=True`` so the evaluator reports
them apart from the ones that stop the bootstrap.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.services.workspace_bootstrap import STAGE_ORDER

#: the resource shapes the stages build — ``resource_name()`` appends ``-<env>`` to the base
#: names, which the trailing ``*`` covers (`core/naming.py`)
_EXEC_ROLE = "arn:{partition}:iam::{account}:role/launchpad-agent-execution-role-{region}*"
_GATEWAY_ROLE = "arn:{partition}:iam::{account}:role/launchpad-gateway-role-{region}*"
_CODEBUILD_ROLE = "arn:{partition}:iam::{account}:role/launchpad-codebuild-role-{region}*"
_SKILL_LAB_ROLE = "arn:{partition}:iam::{account}:role/launchpad-skill-lab-worker-{region}*"
_MEMORY_ADMIN_ROLE = "arn:{partition}:iam::{account}:role/launchpad-memory-admin-*"
_ECR_REPO = "arn:{partition}:ecr:{region}:{account}:repository/launchpad-agents*"
_BUCKET = "arn:{partition}:s3:::launchpad-artifacts-{account}-{region}*"
_BUCKET_OBJECTS = "arn:{partition}:s3:::launchpad-artifacts-{account}-{region}*/*"
_CODEBUILD_PROJECT = "arn:{partition}:codebuild:{region}:{account}:project/launchpad-agent-builder*"
_IDENTITY_SECRET = (
    "arn:{partition}:secretsmanager:{region}:{account}:secret:bedrock-agentcore-identity!*"
)
_PROVIDER_PARAMETER = "arn:{partition}:ssm:{region}:{account}:parameter/launchpad-*-api-key"
_POLICY_LOG_GROUP = "arn:{partition}:logs:{region}:{account}:log-group:/launchpad/policy/decisions*"
_SPANS_LOG_GROUP = "arn:{partition}:logs:{region}:{account}:log-group:aws/spans:*"
# the service-linked-role path uses the partition-invariant service principal (N11-bug5)
_APP_SIGNALS_SLR = (
    "arn:{partition}:iam::{account}:role/aws-service-role/"
    "application-signals.cloudwatch.amazonaws.com/*"  # cn-lint: allow
)
_CLOUDTRAIL_CHANNEL = (
    "arn:{partition}:cloudtrail:{region}:{account}:channel/aws-service-channel/*"
)

_IAM_ROLE_ACTIONS = ("CreateRole", "GetRole", "TagRole", "ListRoleTags", "PutRolePolicy")


@dataclass(frozen=True)
class Need:
    """One IAM action a bootstrap stage issues.

    ``resource`` is the ARN pattern the SERVICE authorises the call against, with the
    workspace's ``{partition}`` / ``{region}`` / ``{account}`` as placeholders; ``"*"`` for an
    action without resource-level permissions, or for a call the stage issues without naming a
    resource (a call that lists without a filter is authorised against the wildcard resource —
    `repository/*` for ECR — which a resource-scoped statement never matches; N17-obs130 moved
    the ``validate-access`` probes onto the by-name shapes for that reason).
    """

    stage: str
    service: str  # the IAM action prefix (`bedrock-agentcore`, not the client name)
    action: str
    resource: str
    source: str  # the function that issues it
    when: str = ""  # a condition the call depends on ("" = every run)
    degrades: bool = False  # True: a denial degrades the stage's feature, never fails the job
    implicit: bool = False  # True: no IAM permission is required (sts:GetCallerIdentity)
    # N17-obs201: the tags the RESOURCE carries when the call is issued (a role the bootstrap
    # created is tagged in its CreateRole call), for a statement whose Condition reads
    # `iam:ResourceTag/<key>`; () = the call is made against an untagged (or unknown) resource.
    tags: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        return f"{self.service}:{self.action}"

    @property
    def label(self) -> str:
        return f"{self.stage} · {self.key} on {self.resource}"

    def render(self, *, partition: str, region: str, account: str) -> str:
        return self.resource.format(partition=partition, region=region, account=account)


#: N17-obs201: every role `workspace_iam.ensure_role` creates carries these tags from its
#: CreateRole call on — before any trust write — so a Condition on `iam:ResourceTag/
#: launchpad:workspace` is satisfied for the bootstrap's own roles and fails for the hub's
#: CDK-made twins (tagged `launchpad:managed` only) and any other untagged role.
_WORKSPACE_ROLE_TAGS: tuple[tuple[str, str], ...] = (
    ("launchpad:workspace", "<workspace id>"),
    ("launchpad:managed", "true"),
)


def _role_needs(stage: str, role: str, source: str) -> list[Need]:
    """`workspace_iam.ensure_role`: create, else adopt (GetRole; ListRoleTags when GetRole
    returns no Tags; TagRole when the owner tag names a retired workspace;
    UpdateAssumeRolePolicy when the stored trust document differs) — then PutRolePolicy.
    The refresh is granted by the template's `IamAdoptWorkspaceRoles` statement, which names
    the four role shapes the bootstrap adopts (N17-obs131 added the gateway and CodeBuild
    roles) under the `launchpad:workspace` tag Condition (N17-obs201)."""
    out = [Need(stage, "iam", action, role, source) for action in _IAM_ROLE_ACTIONS]
    out.append(
        Need(
            stage, "iam", "UpdateAssumeRolePolicy", role, source,
            when="an adopted role whose trust policy differs from the one the stage builds "
                 "(a resumed bootstrap after a trust-policy change)",
            tags=_WORKSPACE_ROLE_TAGS,
        )
    )
    return out


def _probe_needs() -> list[Need]:
    """`_PROBES` + `_caller_identity` + `_refuse_foreign_resources` of `_stage_validate_access`.
    The control-plane and S3 probes list on `*` (`list_gateways(maxResults=1)`,
    `list_buckets()` — no per-resource shape exists and the policy grants both on `*`); ECR,
    CodeBuild and IAM are probed BY NAME against the resource the owning stage creates
    (`describe_repositories(repositoryNames=[<repo>])`, `batch_get_projects(names=[<project>])`,
    `get_role(RoleName=<execution role>)`), a not-found answer counting as usable — the shapes
    the resource-scoped ops policy grants (N17-obs130; the unfiltered lists were authorised
    against `repository/*` / `*`, the shape N17-obs112 recorded live)."""
    s = "validate-access"
    src = "_stage_validate_access"
    return [
        Need(s, "sts", "GetCallerIdentity", "*", f"{src} → _caller_identity", implicit=True),
        Need(s, "bedrock-agentcore", "ListGateways", "*", f"{src} → _probe"),
        Need(s, "s3", "ListAllMyBuckets", "*", f"{src} → _probe (list_buckets)"),
        Need(s, "ecr", "DescribeRepositories", _ECR_REPO,
             f"{src} → _probe (describe_repositories(repositoryNames=[ecr_repo_name()]))"),
        Need(s, "codebuild", "BatchGetProjects", _CODEBUILD_PROJECT,
             f"{src} → _probe (batch_get_projects(names=[codebuild_project_name()]))"),
        Need(s, "iam", "GetRole", _EXEC_ROLE,
             f"{src} → _probe (get_role(RoleName=<regional execution role>))"),
        Need(s, "bedrock-agentcore", "ListGateways", "*",
             f"{src} → _refuse_foreign_resources → _find_gateway"),
        Need(s, "codebuild", "BatchGetProjects", _CODEBUILD_PROJECT,
             f"{src} → _refuse_foreign_resources → _project_exists"),
    ]


def _iam_stage_needs() -> list[Need]:
    src = "_stage_iam → workspace_iam.ensure_role"
    return _role_needs("iam", _EXEC_ROLE, src) + _role_needs("iam", _GATEWAY_ROLE, src)


def _model_credential_needs() -> list[Need]:
    s = "model-credential"
    control = f"{s} → model_credential.ensure_provider_credentials"
    out = [
        Need(s, "bedrock-agentcore", "GetApiKeyCredentialProvider", "*", control),
        Need(s, "bedrock-agentcore", "CreateApiKeyCredentialProvider", "*", control,
             when="first run in the region (the provider does not exist yet)"),
        Need(s, "bedrock-agentcore", "UpdateApiKeyCredentialProvider", "*", control,
             when="re-run (the key is rotated in place)"),
    ]
    # N11-bug7 (live): Identity stores the provider as a Secrets Manager secret in the
    # CALLER's account and authorises Create/Update against the caller.
    secret_src = f"{control} (Identity's own secret, authorised against the caller — N11-bug7)"
    for action in ("CreateSecret", "PutSecretValue", "UpdateSecret", "DescribeSecret",
                   "TagResource"):
        out.append(Need(s, "secretsmanager", action, _IDENTITY_SECRET, secret_src))
    out.append(
        Need(
            s, "ssm", "GetParameter", _PROVIDER_PARAMETER,
            f"{s} → model_credential.ensure_registry_credentials (D21 M1-b)",
            when="a registry provider with credential_source ssm exists",
            degrades=True,  # a ClientError leaves the provider `not configured`, never fails
            # granted on `*` by AmazonSSMManagedInstanceCore (the SSM agent's managed policy),
            # so this one need is region-independent even under the scoped statement
        )
    )
    return out


def _storage_needs() -> list[Need]:
    s = "storage"
    bucket_src = "_stage_storage → _ensure_bucket"
    out = [Need(s, "s3", "CreateBucket", _BUCKET, bucket_src)]
    for action in ("PutBucketPublicAccessBlock", "PutBucketVersioning",
                   "PutEncryptionConfiguration", "PutBucketPolicy"):
        out.append(Need(s, "s3", action, _BUCKET, bucket_src))
    repo_src = "_stage_storage → _ensure_repository"
    out.append(Need(s, "ecr", "DescribeRepositories", _ECR_REPO,
                    f"{repo_src} (describe_repositories(repositoryNames=[<repo>]))"))
    out.append(Need(s, "ecr", "CreateRepository", _ECR_REPO, repo_src,
                    when="first run in the region"))
    return out


def _codebuild_needs() -> list[Need]:
    s = "codebuild"
    out = _role_needs(s, _CODEBUILD_ROLE, "_stage_codebuild → workspace_iam.ensure_role")
    out += [
        Need(s, "codebuild", "BatchGetProjects", _CODEBUILD_PROJECT,
             "_stage_codebuild → _project_exists"),
        Need(s, "codebuild", "CreateProject", _CODEBUILD_PROJECT, "_stage_codebuild",
             when="first run in the region"),
        Need(s, "iam", "PassRole", _CODEBUILD_ROLE,
             "_stage_codebuild (create_project serviceRole=<codebuild role>)",
             when="first run in the region"),
        Need(s, "codebuild", "UpdateProject", _CODEBUILD_PROJECT, "_stage_codebuild"),
    ]
    return out


def _gateway_needs() -> list[Need]:
    s = "gateway"
    src = "_stage_gateway → gateway_bootstrap.ensure_gateway"
    return [
        Need(s, "bedrock-agentcore", "ListGateways", "*", src),
        Need(s, "bedrock-agentcore", "GetGateway", "*", src),
        Need(s, "bedrock-agentcore", "CreateGateway", "*", src,
             when="first run in the region"),
        Need(s, "iam", "PassRole", _GATEWAY_ROLE, f"{src} (create_gateway roleArn=<gateway role>)",
             when="first run in the region"),
    ]


def _registry_needs() -> list[Need]:
    """The Registry is the local ledger (N4): the identifier costs no AWS call. The default
    import (N10-bug1) speaks MCP to the workspace's own gateway and uploads the sample skill —
    best-effort, the stage stays green with the reason in its detail."""
    s = "registry"
    src = "_stage_registry → registry_console.ensure_default_records"
    return [
        Need(s, "bedrock-agentcore", "InvokeGateway", "*",
             f"{src} → mcp_client.tools_list (SigV4 MCP tools/list on the workspace gateway)",
             when="the resource map carries gateway_url", degrades=True),
        Need(s, "s3", "PutObject", _BUCKET_OBJECTS,
             f"{src} → register_skill_bundle (sample skill upload)", degrades=True),
        # N17-obs264 / obs269: the bundle write prunes its `skills/<name>/` prefix to the
        # bundle's files after the upload (list, then delete what it does not ship)
        Need(s, "s3", "ListBucket", _BUCKET,
             f"{src} → _prune_prefix (list skills/<name>/ after the upload)", degrades=True),
        Need(s, "s3", "DeleteObject", _BUCKET_OBJECTS,
             f"{src} → _prune_prefix (DeleteObjects of keys the bundle does not ship)",
             degrades=True),
    ]


def _memory_needs() -> list[Need]:
    """N6 ruling R2: only a workspace in the hub's own account+region inherits the self-hosted
    substrate; anywhere else records `memory_unavailable_reason` with NO AWS call."""
    s = "memory"
    return [
        Need(s, "sts", "AssumeRole", _MEMORY_ADMIN_ROLE,
             "_stage_memory → aws_clients.opensearch_http(admin=<memory_admin_role_arn>)",
             when="hub-colocated workspace only (same account+region as the hub)",
             degrades=False),
    ]


def _skill_lab_needs() -> list[Need]:
    """`skill_lab.infra.ensure_skill_lab_worker`: role → content-addressed image (CodeBuild
    build of the worker Dockerfile from a source zip in the artifacts bucket, ECR digest
    lookup) → runtime. Every failure degrades (`unavailable · <code>`)."""
    s = "skill-lab"
    role_src = "skill_lab.infra.ensure_worker_role → workspace_iam.ensure_role"
    out = [replace(n, degrades=True) for n in _role_needs(s, _SKILL_LAB_ROLE, role_src)]
    build = "skill_lab.worker_build.ensure_worker_image"
    out += [
        Need(s, "s3", "PutObject", _BUCKET_OBJECTS, f"{build} (worker source zip)", degrades=True),
        Need(s, "codebuild", "StartBuild", _CODEBUILD_PROJECT, f"{build} → agentcore/codebuild",
             degrades=True),
        Need(s, "codebuild", "BatchGetBuilds", _CODEBUILD_PROJECT,
             f"{build} → agentcore/codebuild (wait)", degrades=True),
        Need(s, "ecr", "DescribeImages", _ECR_REPO, f"{build} → agentcore/ecr.resolve_digest",
             degrades=True),
    ]
    runtime = "skill_lab.infra.ensure_worker_runtime → agentcore/runtime"
    for action in ("ListAgentRuntimes", "CreateAgentRuntime", "GetAgentRuntime",
                   "UpdateAgentRuntime"):
        out.append(Need(s, "bedrock-agentcore", action, "*", runtime, degrades=True))
    out.append(Need(s, "iam", "PassRole", _SKILL_LAB_ROLE,
                    f"{runtime} (create_agent_runtime roleArn=<worker role>)", degrades=True))
    return out


def _observability_needs() -> list[Need]:
    """Transaction Search is a per-region X-Ray setting; the stage degrades on any
    ClientError / BotoCoreError (`unavailable · <code>`)."""
    s = "observability"
    policy = "_stage_observability → ensure_spans_resource_policy"
    xray = "_stage_observability → observability_bootstrap.ensure_transaction_search"
    out = [
        Need(s, "logs", "DescribeResourcePolicies", "*", policy, degrades=True),
        Need(s, "logs", "PutResourcePolicy", "*", policy,
             when="the region has no TransactionSearchXRayAccess policy yet", degrades=True),
        Need(s, "xray", "GetTraceSegmentDestination", "*", xray, degrades=True),
        Need(s, "xray", "UpdateTraceSegmentDestination", "*", xray,
             when="Transaction Search is not on yet", degrades=True),
    ]
    # N11-bug9 (live): UpdateTraceSegmentDestination is also authorised against the caller
    # for Application Signals discovery, its service-linked role and a CloudTrail channel.
    side = f"{xray} (side effects of UpdateTraceSegmentDestination — N11-bug9 / r2)"
    out += [
        Need(s, "application-signals", "StartDiscovery", "*", side,
             when="Transaction Search is not on yet", degrades=True),
        Need(s, "iam", "CreateServiceLinkedRole", _APP_SIGNALS_SLR, side,
             when="first enablement in the account", degrades=True),
        Need(s, "cloudtrail", "CreateServiceLinkedChannel", _CLOUDTRAIL_CHANNEL, side,
             when="Transaction Search is not on yet", degrades=True),
    ]
    return out


def _policy_needs() -> list[Need]:
    s = "policy"
    src = "_stage_policy → policy_audit.ensure_decision_log_group"
    return [
        Need(s, "logs", "CreateLogGroup", _POLICY_LOG_GROUP, src, degrades=True),
        Need(s, "logs", "PutRetentionPolicy", _POLICY_LOG_GROUP, src,
             when="log_group_retention_days > 0", degrades=True),
    ]


#: stage name → the IAM actions it issues. Every entry of ``STAGE_ORDER`` is a key; `finalize`
#: is ledger-only (it checks the resource map and flips the row to `ready`).
STAGE_NEEDS: dict[str, tuple[Need, ...]] = {
    "validate-access": tuple(_probe_needs()),
    "iam": tuple(_iam_stage_needs()),
    "model-credential": tuple(_model_credential_needs()),
    "storage": tuple(_storage_needs()),
    "codebuild": tuple(_codebuild_needs()),
    "gateway": tuple(_gateway_needs()),
    "registry": tuple(_registry_needs()),
    "memory": tuple(_memory_needs()),
    "skill-lab": tuple(_skill_lab_needs()),
    "observability": tuple(_observability_needs()),
    "policy": tuple(_policy_needs()),
    "finalize": (),
}

assert list(STAGE_NEEDS) == STAGE_ORDER, (list(STAGE_NEEDS), STAGE_ORDER)

#: N17-obs203 §3 — the IAM actions the CONSOLE (a request thread, not a bootstrap stage) issues
#: with the hub's own credentials when a member or administrator types a model-provider API key:
#: `model_probe.ModelProbeHooks.store_api_key` writes + tags the row's SSM SecureString and
#: `forget_api_key` deletes it with the provider; `register_credential` then reads it back (the
#: existing `ReadLaunchpadSecretParameters` grant). Evaluated against the HUB region only (the
#: console always writes to the hub's own account / region, `default_workspace_context`);
#: `tests/consistency/test_workspace_permissions.py` names the granting statement.
CONSOLE_STAGE = "console · model-provider key"
CONSOLE_NEEDS: tuple[Need, ...] = (
    Need(CONSOLE_STAGE, "ssm", "PutParameter", _PROVIDER_PARAMETER,
         "routers/model_providers → ModelProbeHooks.store_api_key"),
    Need(CONSOLE_STAGE, "ssm", "AddTagsToResource", _PROVIDER_PARAMETER,
         "ModelProbeHooks.store_api_key (best-effort tag launchpad:managed)", degrades=True),
    Need(CONSOLE_STAGE, "ssm", "GetParameter", _PROVIDER_PARAMETER,
         "ModelProbeHooks.store_api_key → register_credential (read back for Identity)"),
    Need(CONSOLE_STAGE, "ssm", "DeleteParameter", _PROVIDER_PARAMETER,
         "routers/model_providers DELETE → ModelProbeHooks.forget_api_key", degrades=True),
)

#: Per-user policy stream B3 — the IAM actions the hub-only ``policy-interceptor`` bootstrap stage
#: (`services/policy_interceptor.ensure_interceptor`) issues with the hub's own credentials, in
#: the HUB region (the governed gateway is the hub's `launchpad-gw[-env]`). Not a workspace
#: stage: `tests/consistency/test_workspace_permissions.py` names the granting statements.
INTERCEPTOR_STAGE = "hub · policy-interceptor"
_INTERCEPTOR_FUNCTION = (
    "arn:{partition}:lambda:{region}:{account}:function:launchpad-policy-interceptor*")
_INTERCEPTOR_ROLE = "arn:{partition}:iam::{account}:role/launchpad-policy-interceptor*"
_SRC_INTERCEPTOR = "policy_interceptor.ensure_interceptor"
INTERCEPTOR_NEEDS: tuple[Need, ...] = (
    *(Need(INTERCEPTOR_STAGE, "iam", a, _INTERCEPTOR_ROLE, _SRC_INTERCEPTOR)
      for a in ("CreateRole", "GetRole", "TagRole", "PutRolePolicy", "PassRole")),
    Need(INTERCEPTOR_STAGE, "iam", "PutRolePolicy", _GATEWAY_ROLE,
         f"{_SRC_INTERCEPTOR} (the gateway role's lambda:InvokeFunction grant)"),
    Need(INTERCEPTOR_STAGE, "s3", "PutObject", _BUCKET_OBJECTS,
         f"{_SRC_INTERCEPTOR} → ensure_engine_key (policy/gateway/<gw>/engine-key)"),
    *(Need(INTERCEPTOR_STAGE, "lambda", a, _INTERCEPTOR_FUNCTION, _SRC_INTERCEPTOR)
      for a in ("CreateFunction", "GetFunctionConfiguration", "UpdateFunctionCode",
                "UpdateFunctionConfiguration", "TagResource")),
    *(Need(INTERCEPTOR_STAGE, "ec2", a, "*", f"{_SRC_INTERCEPTOR} (the VpcConfig check)")
      for a in ("DescribeSecurityGroups", "DescribeSubnets", "DescribeVpcs",
                "GetSecurityGroupsForVpc")),
    *(Need(INTERCEPTOR_STAGE, "logs", a,
           "arn:{partition}:logs:{region}:{account}:log-group:/aws/lambda/"
           "launchpad-policy-interceptor*", f"{_SRC_INTERCEPTOR} → ensure_log_group")
      for a in ("CreateLogGroup", "TagResource", "PutRetentionPolicy")),
    Need(INTERCEPTOR_STAGE, "logs", "FilterLogEvents",
         "arn:{partition}:logs:{region}:{account}:log-group:/aws/lambda/"
         "launchpad-policy-interceptor*",
         "policy_audit.GatewayDecisionIngester.poll_once (B5, every 20 s)"),
    Need(INTERCEPTOR_STAGE, "bedrock-agentcore", "UpdateGateway", "*",
         "scripts/attach_policy_interceptor.py → gateway_bootstrap.set_interceptor",
         when="the operator attaches / detaches with the user's go"),
)

#: the sentence `ensure_spans_resource_policy` places as the X-Ray write target — listed for
#: the evaluator's resource table, not issued by the hub (X-Ray writes it)
SPANS_LOG_GROUP = _SPANS_LOG_GROUP


def needs(*, stages: tuple[str, ...] | None = None, include_degrading: bool = True,
          include_conditional: bool = True, include_implicit: bool = False) -> list[Need]:
    """The flattened manifest, filtered."""
    out: list[Need] = []
    for stage in stages or STAGE_ORDER:
        for need in STAGE_NEEDS[stage]:
            if need.implicit and not include_implicit:
                continue
            if need.degrades and not include_degrading:
                continue
            if need.when and not include_conditional:
                continue
            out.append(need)
    return out
