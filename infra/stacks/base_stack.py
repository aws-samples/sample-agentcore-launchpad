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
)
from aws_cdk import (
    aws_apigateway as apigw,
)
from aws_cdk import (
    aws_codebuild as codebuild,
)
from aws_cdk import (
    aws_cognito as cognito,
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
    aws_s3 as s3,
)
from constructs import Construct

SAMPLES_DIR = Path(__file__).resolve().parents[2] / "samples"

DEMO_USERS = [
    {"username": "river", "email": "river@launchpad.local", "group": "platform-admin"},
    {"username": "demo", "email": "demo@launchpad.local", "group": "hr-analyst"},
]

LEGACY_REGION = "us-west-2"


def regional_role_name(base_name: str, region: str) -> str:
    """Keep the existing west stack stable while isolating new regional stacks."""
    return base_name if region == LEGACY_REGION else f"{base_name}-{region}"


class LaunchpadBaseStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ---- artifacts bucket (agent source zips, codebuild inputs) ----
        artifacts = s3.Bucket(
            self,
            "ArtifactsBucket",
            bucket_name=f"launchpad-artifacts-{self.account}-{self.region}",
            versioned=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ---- ECR repo for Claude-SDK container images ----
        repo = ecr.Repository(
            self,
            "AgentsRepo",
            repository_name="launchpad-agents",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
        )

        # ---- CodeBuild: ARM64 image builder (buildspec ships in source zip) ----
        build_project = codebuild.Project(
            self,
            "AgentBuilder",
            project_name="launchpad-agent-builder",
            source=codebuild.Source.s3(bucket=artifacts, path="builds/placeholder/source.zip"),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0,
                compute_type=codebuild.ComputeType.SMALL,
                privileged=True,  # docker build
            ),
            timeout=Duration.minutes(30),
        )
        repo.grant_pull_push(build_project)
        artifacts.grant_read(build_project)

        # ---- Cognito: users, roles for Cedar policy demos ----
        pool = cognito.UserPool(
            self,
            "Users",
            user_pool_name="launchpad-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        client = pool.add_client(
            "Console",
            user_pool_client_name="launchpad-console",
            auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
            id_token_validity=Duration.hours(8),
            access_token_validity=Duration.hours(8),
        )
        pool.add_domain(
            "Domain",
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"launchpad-{self.account}"
            ),
        )
        invoke_scope = cognito.ResourceServerScope(
            scope_name="invoke", scope_description="Invoke launchpad gateway tools"
        )
        resource_server = pool.add_resource_server(
            "GatewayResourceServer",
            identifier="launchpad-gw",
            scopes=[invoke_scope],
        )
        m2m_client = pool.add_client(
            "AgentM2M",
            user_pool_client_name="launchpad-agent-m2m",
            generate_secret=True,
            auth_flows=cognito.AuthFlow(user_password=False, user_srp=False),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(client_credentials=True),
                scopes=[cognito.OAuthScope.resource_server(resource_server, invoke_scope)],
            ),
            access_token_validity=Duration.hours(1),
        )

        for role_name in ("platform-admin", "hr-analyst"):
            cognito.CfnUserPoolGroup(
                self,
                f"Group-{role_name}",
                user_pool_id=pool.user_pool_id,
                group_name=role_name,
                description=f"Launchpad role: {role_name}",
            )

        for spec in DEMO_USERS:
            user = cognito.CfnUserPoolUser(
                self,
                f"User-{spec['username']}",
                user_pool_id=pool.user_pool_id,
                username=spec["username"],
                message_action="SUPPRESS",
                user_attributes=[
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email", value=spec["email"]
                    ),
                    cognito.CfnUserPoolUser.AttributeTypeProperty(
                        name="email_verified", value="true"
                    ),
                ],
            )
            attachment = cognito.CfnUserPoolUserToGroupAttachment(
                self,
                f"Attach-{spec['username']}",
                user_pool_id=pool.user_pool_id,
                group_name=spec["group"],
                username=spec["username"],
            )
            attachment.add_dependency(user)

        # ---- IAM: execution role assumed by AgentCore Runtime workloads ----
        exec_role = iam.Role(
            self,
            "AgentExecutionRole",
            role_name=regional_role_name("launchpad-agent-execution-role", self.region),
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                },
            ),
            description="Assumed by AgentCore Runtime/Harness workloads launched by Launchpad",
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockModels",
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=["*"],
            )
        )
        # Bedrock Mantle (model_source="mantle") is a SEPARATE IAM service from
        # bedrock, despite having no boto3 client and no entry in botocore's
        # endpoints.json — bedrock:InvokeModel does NOT cover it. Without these
        # statements a Mantle agent deploys and reaches ACTIVE, then fails on its
        # first invoke with `401 access_denied … not authorized to perform:
        # bedrock-mantle:CreateInference on … project/default`. Mirrors the AWS
        # managed policy AmazonBedrockMantleInferenceAccess.
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="BedrockMantleInference",
                actions=[
                    "bedrock-mantle:Get*",
                    "bedrock-mantle:List*",
                    "bedrock-mantle:CreateInference",
                ],
                # Mantle models are hosted outside the stack's region, so the
                # region segment stays wildcarded (see LAUNCHPAD_MANTLE_REGION).
                resources=[f"arn:aws:bedrock-mantle:*:{self.account}:project/*"],
            )
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                # Auth is a short-lived bearer token minted from this role by
                # aws_bedrock_token_generator, so this action is not optional.
                sid="BedrockMantleCallWithBearerToken",
                actions=["bedrock-mantle:CallWithBearerToken"],
                resources=["*"],
            )
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                # Third-party Mantle models (the openai.* / xai.* families) are
                # fronted by Marketplace subscriptions. Scoped by CalledViaLast so
                # the role cannot subscribe to anything on its own initiative.
                sid="MarketplaceOperationsFromBedrockMantleFor3pModels",
                actions=["aws-marketplace:Subscribe", "aws-marketplace:ViewSubscriptions"],
                resources=["*"],
                conditions={
                    "StringEquals": {"aws:CalledViaLast": "bedrock-mantle.amazonaws.com"}
                },
            )
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="AgentCoreDataPlane",
                actions=[
                    "bedrock-agentcore:CreateEvent",
                    "bedrock-agentcore:GetEvent",
                    "bedrock-agentcore:ListEvents",
                    "bedrock-agentcore:ListSessions",
                    "bedrock-agentcore:ListActors",
                    "bedrock-agentcore:RetrieveMemoryRecords",
                    "bedrock-agentcore:GetMemoryRecord",
                    "bedrock-agentcore:ListMemoryRecords",
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
                sid="ABTestOrchestration",
                actions=[
                    "bedrock-agentcore:GetGateway",
                    "bedrock-agentcore:GetGatewayTarget",
                    "bedrock-agentcore:ListGatewayTargets",
                    "bedrock-agentcore:InvokeAgentRuntime",
                    "bedrock-agentcore:GetConfigurationBundle",
                    "bedrock-agentcore:GetConfigurationBundleVersion",
                    "bedrock-agentcore:GetOnlineEvaluationConfig",
                    "bedrock-agentcore:GetABTest",
                    "bedrock-agentcore:StartBatchEvaluation",
                    "bedrock-agentcore:GetBatchEvaluation",
                    # A/B tests manage routing rules on the experiment gateway
                    "bedrock-agentcore:CreateGatewayRule",
                    "bedrock-agentcore:GetGatewayRule",
                    "bedrock-agentcore:UpdateGatewayRule",
                    "bedrock-agentcore:DeleteGatewayRule",
                    "bedrock-agentcore:ListGatewayRules",
                    "bedrock-agentcore:UpdateGateway",
                ],
                resources=["*"],
            )
        )
        # Managed KB retrieval for the code-generating methods (zip_runtime /
        # container): their generated kb_search / kb_deep_search tools call the
        # Bedrock data plane directly with this role instead of going through
        # launchpad-kb-gw, which only a managed Harness can attach.
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="ManagedKbRetrieval",
                actions=["bedrock:Retrieve", "bedrock:GetKnowledgeBase"],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"
                ],
            )
        )
        # kb_deep_search (agentic multi-step retrieval). Kept as its own statement
        # so the '*' resource is visible in isolation to anyone reading the role:
        # AgenticRetrieveStream is NOT resource-scopable, so every Launchpad
        # runtime can agentic-retrieve against any KB in the account. Accepted —
        # launchpad-gateway-role already carries the same grant for the harness
        # channel. The MANAGED foundation/reranking model types mean the service
        # supplies the planner, so no extra model grant is needed beyond the
        # account-wide bedrock:InvokeModel above.
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="ManagedKbAgenticRetrieval",
                actions=["bedrock:AgenticRetrieveStream"],
                resources=["*"],
            )
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                sid="IdentityVaultSecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore-identity!*"
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
            function_name="launchpad-hr-database",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(SAMPLES_DIR / "hr_database_lambda")),
            timeout=Duration.seconds(15),
            description="Launchpad sample: HR database exposed as MCP tools",
        )

        # ---- samples: office-facts REST API (→ MCP via OpenAPI target) ----
        facts_lambda = lambda_.Function(
            self,
            "OfficeFacts",
            function_name="launchpad-office-facts",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset(str(SAMPLES_DIR / "rest_api")),
            timeout=Duration.seconds(10),
            description="Launchpad sample: office-facts REST backend",
        )
        facts_api = apigw.LambdaRestApi(
            self,
            "OfficeFactsApi",
            rest_api_name="launchpad-office-facts",
            handler=facts_lambda,
            proxy=False,
            deploy_options=apigw.StageOptions(stage_name="prod"),
        )
        facts = facts_api.root.add_resource("facts")
        facts.add_method("GET", api_key_required=True)
        topic = facts.add_resource("{topic}")
        topic.add_method("GET", api_key_required=True)
        api_key = facts_api.add_api_key("OfficeFactsKey", api_key_name="launchpad-office-facts")
        plan = facts_api.add_usage_plan(
            "OfficeFactsPlan",
            name="launchpad-office-facts",
            throttle=apigw.ThrottleSettings(rate_limit=10, burst_limit=20),
        )
        plan.add_api_key(api_key)
        plan.add_api_stage(stage=facts_api.deployment_stage)

        # ---- gateway service role (assumed by AgentCore Gateway) ----
        gateway_role = iam.Role(
            self,
            "GatewayRole",
            role_name=regional_role_name("launchpad-gateway-role", self.region),
            assumed_by=iam.ServicePrincipal(
                "bedrock-agentcore.amazonaws.com",
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
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*"
                ],
            )
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="PolicyEngineEvaluation",
                actions=[
                    "bedrock-agentcore:GetPolicyEngine",
                    "bedrock-agentcore:GetPolicy",
                    "bedrock-agentcore:ListPolicies",
                    "bedrock-agentcore:ListPolicySummaries",
                    "bedrock-agentcore:AuthorizeAction",
                    "bedrock-agentcore:PartiallyAuthorizeActions",
                    "bedrock-agentcore:BatchAuthorizeActions",
                ],
                resources=[
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:policy-engine/*",
                    f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:gateway/*",
                ],
            )
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="IdentitySecrets",
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:bedrock-agentcore-identity!*"
                ],
            )
        )
        # Managed KB connector targets: the gateway validates bound KBs at target
        # creation (GetKnowledgeBase) and retrieves on behalf of agents.
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="ManagedKbRetrieval",
                actions=["bedrock:GetKnowledgeBase", "bedrock:Retrieve"],
                resources=[
                    f"arn:aws:bedrock:{self.region}:{self.account}:knowledge-base/*"
                ],
            )
        )
        gateway_role.add_to_policy(
            iam.PolicyStatement(
                sid="ManagedKbAgenticRetrieval",
                # AgenticRetrieveStream is not resource-scoped.
                actions=["bedrock:AgenticRetrieveStream"],
                resources=["*"],
            )
        )

        # ---- knowledge base service role (assumed by Bedrock Managed KB) ----
        # CreateKnowledgeBase requires a roleArn; managed KBs use it to read S3
        # data sources. Base grant covers the artifacts bucket kb/ prefix;
        # per-KB inline policies (launchpad-kb-{id}) extend it to external buckets.
        kb_role = iam.Role(
            self,
            "KnowledgeBaseRole",
            role_name=regional_role_name("launchpad-kb-role", self.region),
            assumed_by=iam.ServicePrincipal(
                "bedrock.amazonaws.com",
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            ),
            description="Assumed by Bedrock Managed Knowledge Bases to ingest S3 data sources",
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="KbDataObjects",
                actions=["s3:GetObject"],
                resources=[artifacts.arn_for_objects("kb/*")],
            )
        )
        kb_role.add_to_policy(
            iam.PolicyStatement(
                sid="KbDataList",
                actions=["s3:ListBucket"],
                resources=[artifacts.bucket_arn],
                conditions={"StringLike": {"s3:prefix": "kb/*"}},
            )
        )

        # ---- outputs consumed by backend bootstrap ----
        CfnOutput(self, "HrLambdaArn", value=hr_lambda.function_arn)
        CfnOutput(self, "OfficeFactsApiUrl", value=facts_api.url)
        CfnOutput(self, "OfficeFactsApiKeyId", value=api_key.key_id)
        CfnOutput(self, "GatewayRoleArn", value=gateway_role.role_arn)
        CfnOutput(self, "KbRoleArn", value=kb_role.role_arn)
        CfnOutput(self, "ArtifactsBucketName", value=artifacts.bucket_name)
        CfnOutput(self, "EcrRepoName", value=repo.repository_name)
        CfnOutput(self, "EcrRepoUri", value=repo.repository_uri)
        CfnOutput(self, "CodeBuildProjectName", value=build_project.project_name)
        CfnOutput(self, "CodeBuildRoleArn", value=build_project.role.role_arn)
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=client.user_pool_client_id)
        CfnOutput(self, "M2MClientId", value=m2m_client.user_pool_client_id)
        CfnOutput(self, "AgentExecutionRoleArn", value=exec_role.role_arn)
