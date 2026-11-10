"""LaunchpadBaseStack — shared substrate every deploy path reuses.

Per-agent resources (runtimes, harnesses) are created by the platform's
boto3 fast path and tracked in the ledger; only account-shared resources
live here.
"""

from pathlib import Path

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
)
from aws_cdk import (
    aws_apigateway as apigw,
)
from aws_cdk import (
    aws_codebuild as codebuild,
)
from aws_cdk import (
    aws_ecr as ecr,
)
from aws_cdk import (
    aws_iam as iam,
)
from aws_cdk import (
    aws_lambda as lambda_,
)
from aws_cdk import (
    aws_logs as logs,
)
from aws_cdk import (
    aws_s3 as s3,
)
from constructs import Construct

from stacks.naming import env_name_from_context, suffixed
from stacks.principals import service_principal

# N11-obs56: every Lambda the stacks create logs into an explicit, retention-bounded group
# (90 days — the backend's `Settings.log_group_retention_days` default for the per-runtime
# groups). Without one the service auto-creates `/aws/lambda/<function>` with no retention.
LAMBDA_LOG_RETENTION = logs.RetentionDays.THREE_MONTHS


def lambda_log_group(scope: Construct, construct_id: str) -> logs.LogGroup:
    """The log group a stack Lambda is pointed at (`Function(log_group=...)`).

    CDK-named on purpose: a stack deployed before this change already has the
    service-created `/aws/lambda/<function>` group, which CloudFormation would refuse to
    create a second time; the old group simply stops receiving lines (delete it by hand
    to reclaim it — deploy.md §8.4). Deleted with the stack."""
    return logs.LogGroup(
        scope, construct_id, retention=LAMBDA_LOG_RETENTION, removal_policy=RemovalPolicy.DESTROY
    )

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"

# Legacy bare role names apply only when the stack region is the original Global one
# (never true in aws-cn; round-3 cleanup candidate).
# region-boundary: allow legacy bare-role-name rule, inert in aws-cn
LEGACY_REGION = "us-west-2"

# N11-r3-bug7: the CodeBuild projects resolve from the BUILD mirror, not the host mirror
# (tuna, `Settings.pip_index_url` / EC2 `PipIndexUrl`): tuna answers CodeBuild's NAT egress
# with an immediate 4xx while the EC2 hosts get 200 from the same URL. NJU is the only
# independent uv-complete mirror probed (N11-obs27); Tencent is the fallback the worker
# Dockerfile retries on. Mirrors Settings.codebuild_pip_index_url / _fallback_index_url.
DEFAULT_CODEBUILD_PIP_INDEX_URL = "https://mirror.nju.edu.cn/pypi/web/simple"
DEFAULT_CODEBUILD_PIP_FALLBACK_INDEX_URL = "https://mirrors.cloud.tencent.com/pypi/simple"
DEFAULT_NPM_REGISTRY = "https://registry.npmmirror.com"
DEFAULT_BASE_IMAGE_MIRROR_PREFIX = "base"


def regional_role_name(base_name: str, region: str) -> str:
    """Keep the existing west stack stable while isolating new regional stacks."""
    return base_name if region == LEGACY_REGION else f"{base_name}-{region}"


class LaunchpadBaseStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # `-c env_name=<x>` (stacks/naming.py): every fixed physical name below goes
        # through `name()` so a parallel environment gets `-<x>` appended; empty
        # (the default) leaves every name byte-for-byte as before.
        self.env_name = env_name_from_context(self.node)

        def name(base: str) -> str:
            return suffixed(base, self.env_name)

        # ---- artifacts bucket (agent source zips, codebuild inputs) ----
        # Exposed as `self.artifacts` (and `self.exec_role` below) so the sibling
        # `launchpad-memory` stack (N6) can grant itself scoped read/write on the
        # memory/ and kb/ prefixes without this stack knowing that stack exists.
        artifacts = s3.Bucket(
            self,
            "ArtifactsBucket",
            bucket_name=name(f"launchpad-artifacts-{self.account}-{self.region}"),
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        self.artifacts = artifacts
        # Per-user policy stream B (lead review D2): the gateway engine HMAC key objects
        # (policy/gateway/<gw>/engine-key, contract amendment 3) are readable ONLY by the
        # interceptor Lambda's role and this environment's hub instance role (which installs
        # the key for the engine). Several grants reach `<bucket>/*` — the BYOC CodeBuild
        # project (it runs member Dockerfiles), workspace source-object reads — so an
        # explicit Deny overrides every Allow for everyone else, administrators included.
        artifacts.add_to_resource_policy(iam.PolicyStatement(
            sid="EngineKeyReadersOnly",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[artifacts.arn_for_objects("policy/gateway/*/engine-key")],
            conditions={"StringNotLike": {"aws:PrincipalArn": [
                f"arn:{self.partition}:iam::{self.account}:role/"
                + name("launchpad-policy-interceptor"),
                f"arn:{self.partition}:iam::{self.account}:role/"
                + name(f"launchpad-cn-ops-{self.region}"),
            ]}},
        ))

        # Per-user policy D5 (contract I9): the eval / canary principal tokens
        # (evaluations/<job_id>/tokens.json — bearer material the hub rewrites while a job
        # runs) are readable ONLY by the evaluation worker's execution role and this
        # environment's hub role; the same bucket-wide readers as above are denied.
        # The worker role is `launchpad-agent-<preset name>-<agent id[:8]>[-<env>]`
        # (backend agent_iam.role_name_for; the preset name carries the env suffix too).
        eval_worker = name("launchpad-eval-worker")
        worker_role = (f"launchpad-agent-{eval_worker}-????????"
                       + (f"-{self.env_name}" if self.env_name else ""))
        artifacts.add_to_resource_policy(iam.PolicyStatement(
            sid="EvalTokensReadersOnly",
            effect=iam.Effect.DENY,
            principals=[iam.AnyPrincipal()],
            actions=["s3:GetObject", "s3:GetObjectVersion"],
            resources=[artifacts.arn_for_objects("evaluations/*/tokens.json")],
            conditions={"StringNotLike": {"aws:PrincipalArn": [
                f"arn:{self.partition}:iam::{self.account}:role/{worker_role}",
                f"arn:{self.partition}:iam::{self.account}:role/"
                + name(f"launchpad-cn-ops-{self.region}"),
            ]}},
        ))

        # ---- ECR repo for BYOC container images (CodeBuild output) ----
        # image_tag_mutability is deliberately left MUTABLE. The BYOC
        # container_source path tags images `{agent}-v{version}`, and
        # _stage_package runs BEFORE _stage_deploy bumps the version — so a
        # re-publish pushes the same tag twice and IMMUTABLE would fail that
        # second push. What matters is that deployment references the image by
        # *digest* (deployer/image_build.py), so the mutable tag is cosmetic.
        repo = ecr.Repository(
            self,
            "AgentsRepo",
            repository_name=name("launchpad-agents"),
            # Scan on push so a deploy can be blocked on findings before the image
            # ever backs a runtime.
            image_scan_on_push=True,
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
        )

        # ---- CodeBuild: ARM64 image builder (buildspec ships in source zip) ----
        pip_index_url = (
            self.node.try_get_context("codebuild_pip_index_url")
            or DEFAULT_CODEBUILD_PIP_INDEX_URL
        )
        pip_fallback_index_url = (
            self.node.try_get_context("codebuild_pip_fallback_index_url")
            or DEFAULT_CODEBUILD_PIP_FALLBACK_INDEX_URL
        )
        npm_registry = self.node.try_get_context("npm_registry") or DEFAULT_NPM_REGISTRY
        base_image_mirror_enabled = str(
            self.node.try_get_context("base_image_mirror_enabled") or "false"
        ).lower() == "true"
        base_image_mirror_prefix = (
            self.node.try_get_context("base_image_mirror_prefix")
            or DEFAULT_BASE_IMAGE_MIRROR_PREFIX
        )
        codebuild_env_vars: dict[str, codebuild.BuildEnvironmentVariable] = {
            "PIP_INDEX_URL": codebuild.BuildEnvironmentVariable(value=pip_index_url),
            # uv 0.9.10 deprecates UV_INDEX_URL in favor of UV_DEFAULT_INDEX —
            # set both so the mirror applies regardless of which uv release runs.
            "UV_INDEX_URL": codebuild.BuildEnvironmentVariable(value=pip_index_url),
            "UV_DEFAULT_INDEX": codebuild.BuildEnvironmentVariable(value=pip_index_url),
            # the Dockerfiles retry on this one when the primary refuses the build egress
            "PIP_FALLBACK_INDEX_URL": codebuild.BuildEnvironmentVariable(
                value=pip_fallback_index_url
            ),
            "NPM_CONFIG_REGISTRY": codebuild.BuildEnvironmentVariable(value=npm_registry),
        }
        pip_extra_index_url = str(self.node.try_get_context("pip_extra_index_url") or "")
        if pip_extra_index_url:
            # N11-obs25: optional secondary index for mirror outages — must be
            # uv-complete (NJU is; Aliyun 404s its PEP 658 metadata); pip + uv spellings.
            codebuild_env_vars["PIP_EXTRA_INDEX_URL"] = codebuild.BuildEnvironmentVariable(
                value=pip_extra_index_url
            )
            codebuild_env_vars["UV_INDEX"] = codebuild.BuildEnvironmentVariable(
                value=pip_extra_index_url
            )
        if base_image_mirror_enabled:
            codebuild_env_vars["BASE_IMAGE"] = codebuild.BuildEnvironmentVariable(
                value=(
                    f"{self.account}.dkr.ecr.{self.region}.{self.url_suffix}/"
                    f"{base_image_mirror_prefix}/python:3.13-slim"
                )
            )
        build_project = codebuild.Project(
            self,
            "AgentBuilder",
            project_name=name("launchpad-agent-builder"),
            source=codebuild.Source.s3(bucket=artifacts, path="builds/placeholder/source.zip"),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
                compute_type=codebuild.ComputeType.SMALL,
                privileged=True,  # docker build
                environment_variables=codebuild_env_vars,
            ),
            timeout=Duration.minutes(30),
        )
        repo.grant_pull_push(build_project)
        artifacts.grant_read(build_project)
        # N17-obs465: the aws-cn trace exporter layer is built FROM a member's existing image
        # (BYOC container_image) — pull only, this account + region's repositories
        build_project.add_to_role_policy(
            iam.PolicyStatement(
                sid="EcrPullMemberImages",
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:BatchGetImage",
                    "ecr:GetDownloadUrlForLayer",
                ],
                resources=[f"arn:{self.partition}:ecr:{self.region}:{self.account}:repository/*"],
            )
        )

        # ---- IAM: execution role assumed by AgentCore Runtime workloads ----
        exec_role = iam.Role(
            self,
            "AgentExecutionRole",
            role_name=name(regional_role_name("launchpad-agent-execution-role", self.region)),
            assumed_by=service_principal(
                "bedrock-agentcore",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                },
            ),
            description="Assumed by AgentCore Runtime/Harness workloads launched by Launchpad",
        )
        self.exec_role = exec_role
        # The platform trusts an execution role for additive grants only when it carries
        # this marker (workspace roles created by the backend carry it too); scoped to
        # this one construct so the gateway/KB/build roles stay unmarked.
        Tags.of(exec_role).add("launchpad:managed", "true")
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreDataPlane",
                actions=[
                    "bedrock-agentcore:GetResourceApiKey",
                    "bedrock-agentcore:GetResourceOauth2Token",
                    "bedrock-agentcore:GetWorkloadAccessToken",
                    "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                    "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                    "bedrock-agentcore:InvokeCodeInterpreter",
                    "bedrock-agentcore:StartCodeInterpreterSession",
                    "bedrock-agentcore:StopCodeInterpreterSession",
                    "bedrock-agentcore:GetCodeInterpreterSession",
                    "bedrock-agentcore:ConnectBrowserAutomationStream",
                    "bedrock-agentcore:ConnectBrowserLiveViewStream",
                    "bedrock-agentcore:StartBrowserSession",
                    "bedrock-agentcore:StopBrowserSession",
                    "bedrock-agentcore:GetBrowserSession",
                ],
                resources=["*"],
            )
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrPull",
                actions=[
                    "ecr:GetDownloadUrlForLayer",
                    "ecr:BatchGetImage",
                ],
                resources=[repo.repository_arn],
            )
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="EcrAuth",
                actions=["ecr:GetAuthorizationToken"],
                resources=["*"],
            )
        )
        # Harness runtimes fetch attached skill bundles ({"s3": {"uri": …}})
        # from the artifacts bucket under skills/ using this role.
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="SkillBundleObjects",
                actions=["s3:GetObject"],
                resources=[artifacts.arn_for_objects("skills/*")],
            )
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="SkillBundleList",
                actions=["s3:ListBucket"],
                resources=[artifacts.bucket_arn],
                conditions={"StringLike": {"s3:prefix": "skills/*"}},
            )
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="GatewayAndRuntimeRead",
                actions=[
                    "bedrock-agentcore:GetGateway",
                    "bedrock-agentcore:GetGatewayTarget",
                    "bedrock-agentcore:ListGatewayTargets",
                    "bedrock-agentcore:InvokeAgentRuntime",
                    # N12-bug5: an invoke that carries runtimeUserId (every Launchpad
                    # caller does) is authorized as the ForUser action — same pair as
                    # the gateway role's InvokeRuntimeTargets and the per-agent policies
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                    # Chat's explicit END SESSION (StopRuntimeSession) — a console
                    # action, granted next to the console's InvokeAgentRuntime
                    "bedrock-agentcore:StopRuntimeSession",
                ],
                resources=["*"],
            )
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="IdentityVaultSecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:{self.partition}:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore-identity!*"
                ],
            )
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="Telemetry",
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                    "logs:StartQuery",
                    "logs:GetQueryResults",
                    "logs:StopQuery",
                    "logs:FilterLogEvents",
                    "logs:GetLogEvents",
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                    "cloudwatch:PutMetricData",
                ],
                resources=["*"],
            )
        )

        # ---- samples: hr-database Lambda (→ MCP tools via Gateway) ----
        hr_lambda = lambda_.Function(
            self,
            "HrDatabase",
            function_name=name("launchpad-hr-database"),
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(SAMPLES_DIR / "hr_database_lambda")),
            timeout=Duration.seconds(15),
            description="Launchpad sample: HR database exposed as MCP tools",
            log_group=lambda_log_group(self, "HrDatabaseLogs"),
        )

        # ---- samples: office-facts REST API (→ MCP via OpenAPI target) ----
        facts_lambda = lambda_.Function(
            self,
            "OfficeFacts",
            function_name=name("launchpad-office-facts"),
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(SAMPLES_DIR / "rest_api")),
            timeout=Duration.seconds(10),
            description="Launchpad sample: office-facts REST backend",
            log_group=lambda_log_group(self, "OfficeFactsLogs"),
        )
        facts_api = apigw.LambdaRestApi(
            self,
            "OfficeFactsApi",
            rest_api_name=name("launchpad-office-facts"),
            handler=facts_lambda,
            proxy=False,
            deploy_options=apigw.StageOptions(stage_name="prod"),
            # N11 r3 (live): the L2 default endpoint type is EDGE, which the
            # aws-cn partition rejects ("Endpoint Configuration type EDGE is
            # not supported in this region: cn-northwest-1") and rolls the
            # whole launchpad-base stack back. REGIONAL is what China offers.
            endpoint_configuration=apigw.EndpointConfiguration(
                types=[apigw.EndpointType.REGIONAL]
            ),
        )
        facts = facts_api.root.add_resource("facts")
        facts.add_method("GET", api_key_required=True)
        topic = facts.add_resource("{topic}")
        topic.add_method("GET", api_key_required=True)
        api_key = facts_api.add_api_key(
            "OfficeFactsKey", api_key_name=name("launchpad-office-facts")
        )
        plan = facts_api.add_usage_plan(
            "OfficeFactsPlan",
            name=name("launchpad-office-facts"),
            throttle=apigw.ThrottleSettings(rate_limit=10, burst_limit=20),
        )
        plan.add_api_key(api_key)
        plan.add_api_stage(stage=facts_api.deployment_stage)

        # ---- gateway service role (assumed by AgentCore Gateway) ----
        gateway_role = iam.Role(
            self,
            "GatewayRole",
            role_name=name(regional_role_name("launchpad-gateway-role", self.region)),
            assumed_by=service_principal(
                "bedrock-agentcore",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="Assumed by AgentCore Gateway to reach targets + identity vault",
        )
        hr_lambda.grant_invoke(gateway_role)
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeRestTargets",
                actions=["execute-api:Invoke"],
                resources=[facts_api.arn_for_execute_api()],
            )
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="IdentityVault",
                actions=[
                    "bedrock-agentcore:GetResourceApiKey",
                    "bedrock-agentcore:GetResourceOauth2Token",
                    "bedrock-agentcore:GetWorkloadAccessToken",
                ],
                resources=["*"],
            )
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="InvokeRuntimeTargets",
                actions=[
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:InvokeAgentRuntimeForUser",
                ],
                resources=[
                    f"arn:{self.partition}:bedrock-agentcore:{self.region}:{self.account}:runtime/*"
                ],
            )
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="IdentitySecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:{self.partition}:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore-identity!*"
                ],
            )
        )

        # ---- outputs consumed by backend bootstrap ----
        CfnOutput(self, "HrLambdaArn", value=hr_lambda.function_arn)
        CfnOutput(self, "OfficeFactsApiUrl", value=facts_api.url)
        CfnOutput(self, "OfficeFactsApiKeyId", value=api_key.key_id)
        CfnOutput(self, "GatewayRoleArn", value=gateway_role.role_arn)
        CfnOutput(self, "ArtifactsBucketName", value=artifacts.bucket_name)
        CfnOutput(self, "EcrRepoName", value=repo.repository_name)
        CfnOutput(self, "EcrRepoUri", value=repo.repository_uri)
        CfnOutput(self, "CodeBuildProjectName", value=build_project.project_name)
        CfnOutput(self, "CodeBuildRoleArn", value=build_project.role.role_arn)
        CfnOutput(self, "AgentExecutionRoleArn", value=exec_role.role_arn)
