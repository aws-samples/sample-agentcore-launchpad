"""Workspace bootstrap job: stages, resume, refusals, and the admin route.

Every AWS call goes to a stateful fake that behaves like the real API for the
one property the stages depend on — create-then-list returns what was created —
so "run it twice and nothing new appears" is a real idempotency assertion rather
than a mock-call count.

Nothing here touches AWS. The live end-to-end (a second workspace bootstrapped
from the console) is the task's final gate and runs separately.
"""

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import pipeline
from app.main import create_app
from app.models.ledger import Job, Workspace
from app.services import aws_clients, workspace_iam
from app.services import users as users_service
from app.services import workspace_bootstrap as wb
from app.services.workspace import context_for_workspace, merge_workspace_resources

ACCOUNT = "444455556666"
REGION = "us-west-1"
WS_ID = "acct-usw1"

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}
MEMBER_CREDS = {
    "username": "ws-bootstrap-member",
    "email": "ws-bootstrap-member@acme-corp.com",
    "password": "sufficient-pass",
}


def _client_error(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} (fake)"}}, operation)


def _actions(statement: dict) -> list[str]:
    action = statement["Action"]
    return [action] if isinstance(action, str) else list(action)


# ── the fake AWS surface ───────────────────────────────────────────────────


class FakeSts:
    def __init__(self) -> None:
        self.account = ACCOUNT

    def get_caller_identity(self):
        return {"Account": self.account, "Arn": f"arn:aws:iam::{self.account}:root"}


class FakeIam:
    def __init__(self) -> None:
        self.roles: dict[str, dict] = {}
        self.created: list[str] = []
        self.inline: dict[tuple[str, str], str] = {}
        self.retagged: list[str] = []

    def preexisting(self, name: str, tags: dict[str, str] | None = None) -> None:
        """A role that already exists — untagged stands in for a CDK-made one."""
        self.roles[name] = {
            "arn": f"arn:aws:iam::{ACCOUNT}:role/{name}",
            "tags": dict(tags or {}),
            "trust": "{}",
        }

    def list_roles(self, **_kw):
        return {"Roles": []}

    def create_role(self, RoleName, AssumeRolePolicyDocument, Description, Tags):  # noqa: N803
        if RoleName in self.roles:
            raise _client_error("EntityAlreadyExists", "CreateRole")
        self.preexisting(RoleName, {tag["Key"]: tag["Value"] for tag in Tags})
        self.roles[RoleName]["trust"] = AssumeRolePolicyDocument
        self.created.append(RoleName)
        return {"Role": {"Arn": self.roles[RoleName]["arn"]}}

    def get_role(self, RoleName):  # noqa: N803
        return {"Role": {"Arn": self.roles[RoleName]["arn"]}}

    def list_role_tags(self, RoleName):  # noqa: N803
        return {
            "Tags": [
                {"Key": key, "Value": value}
                for key, value in self.roles[RoleName]["tags"].items()
            ]
        }

    def tag_role(self, RoleName, Tags):  # noqa: N803
        self.roles[RoleName]["tags"].update({t["Key"]: t["Value"] for t in Tags})
        self.retagged.append(RoleName)

    def update_assume_role_policy(self, RoleName, PolicyDocument):  # noqa: N803
        self.roles[RoleName]["trust"] = PolicyDocument

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):  # noqa: N803
        self.inline[(RoleName, PolicyName)] = PolicyDocument


class FakeS3:
    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.created: list[dict] = []
        self.settings: dict[str, dict] = {}

    def list_buckets(self):
        return {"Buckets": [{"Name": name} for name in sorted(self.buckets)]}

    def create_bucket(self, Bucket, CreateBucketConfiguration=None):  # noqa: N803
        if Bucket in self.buckets:
            raise _client_error("BucketAlreadyOwnedByYou", "CreateBucket")
        self.buckets.add(Bucket)
        self.created.append(
            {
                "bucket": Bucket,
                "location": (CreateBucketConfiguration or {}).get("LocationConstraint"),
            }
        )
        return {}

    def _put(self, bucket: str, key: str, value) -> None:
        self.settings.setdefault(bucket, {})[key] = value

    def put_public_access_block(self, Bucket, PublicAccessBlockConfiguration):  # noqa: N803
        self._put(Bucket, "public_access_block", PublicAccessBlockConfiguration)

    def put_bucket_versioning(self, Bucket, VersioningConfiguration):  # noqa: N803
        self._put(Bucket, "versioning", VersioningConfiguration)

    def put_bucket_encryption(self, Bucket, ServerSideEncryptionConfiguration):  # noqa: N803
        self._put(Bucket, "encryption", ServerSideEncryptionConfiguration)

    def put_bucket_policy(self, Bucket, Policy):  # noqa: N803
        self._put(Bucket, "policy", Policy)


class FakeEcr:
    def __init__(self) -> None:
        self.repos: dict[str, dict] = {}
        self.created: list[str] = []

    def describe_repositories(self, repositoryNames=None, maxResults=None):  # noqa: N803
        if repositoryNames:
            name = repositoryNames[0]
            if name not in self.repos:
                raise _client_error("RepositoryNotFoundException", "DescribeRepositories")
            return {"repositories": [self.repos[name]]}
        return {"repositories": list(self.repos.values())}

    def create_repository(self, repositoryName, imageScanningConfiguration=None):  # noqa: N803
        repo = {
            "repositoryName": repositoryName,
            "repositoryUri": (
                f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/{repositoryName}"
            ),
            "scan": imageScanningConfiguration,
        }
        self.repos[repositoryName] = repo
        self.created.append(repositoryName)
        return {"repository": repo}


class FakeCodeBuild:
    def __init__(self) -> None:
        self.projects: dict[str, dict] = {}
        self.created: list[str] = []

    def list_projects(self):
        return {"projects": sorted(self.projects)}

    def batch_get_projects(self, names):
        return {"projects": [self.projects[n] for n in names if n in self.projects]}

    def create_project(self, **kwargs):
        self.projects[kwargs["name"]] = kwargs
        self.created.append(kwargs["name"])
        return {"project": kwargs}


class FakeCognito:
    def __init__(self) -> None:
        self.pools: dict[str, dict] = {}
        self.created_pools: list[str] = []
        self.created_clients: list[str] = []
        self.domains: dict[str, str] = {}

    def preexisting_pool(self, name: str) -> str:
        pool_id = f"{REGION}_{len(self.pools) + 1}"
        self.pools[pool_id] = {
            "Name": name,
            "clients": {},
            "resource_servers": set(),
            "groups": set(),
        }
        return pool_id

    def list_user_pools(self, MaxResults=None, NextToken=None):  # noqa: N803
        return {
            "UserPools": [
                {"Id": pool_id, "Name": pool["Name"]}
                for pool_id, pool in self.pools.items()
            ]
        }

    def create_user_pool(self, PoolName, **_kwargs):  # noqa: N803
        pool_id = self.preexisting_pool(PoolName)
        self.created_pools.append(PoolName)
        return {"UserPool": {"Id": pool_id, "Name": PoolName}}

    def describe_resource_server(self, UserPoolId, Identifier):  # noqa: N803
        if Identifier not in self.pools[UserPoolId]["resource_servers"]:
            raise _client_error("ResourceNotFoundException", "DescribeResourceServer")
        return {"ResourceServer": {"Identifier": Identifier}}

    def create_resource_server(self, UserPoolId, Identifier, Name, Scopes):  # noqa: N803
        self.pools[UserPoolId]["resource_servers"].add(Identifier)

    def list_user_pool_clients(self, UserPoolId, MaxResults=None, NextToken=None):  # noqa: N803
        return {
            "UserPoolClients": [
                {"ClientId": client["ClientId"], "ClientName": name}
                for name, client in self.pools[UserPoolId]["clients"].items()
            ]
        }

    def create_user_pool_client(self, UserPoolId, ClientName, **kwargs):  # noqa: N803
        client = {"ClientId": f"cid-{ClientName}", "ClientSecret": "s3cr3t", **kwargs}
        self.pools[UserPoolId]["clients"][ClientName] = client
        self.created_clients.append(ClientName)
        return {"UserPoolClient": client}

    def describe_user_pool_client(self, UserPoolId, ClientId):  # noqa: N803
        for client in self.pools[UserPoolId]["clients"].values():
            if client["ClientId"] == ClientId:
                return {"UserPoolClient": client}
        raise _client_error("ResourceNotFoundException", "DescribeUserPoolClient")

    def create_group(self, UserPoolId, GroupName, Description=None):  # noqa: N803
        if GroupName in self.pools[UserPoolId]["groups"]:
            raise _client_error("GroupExistsException", "CreateGroup")
        self.pools[UserPoolId]["groups"].add(GroupName)

    def describe_user_pool_domain(self, Domain):  # noqa: N803
        # Cognito answers an unknown domain with an empty description, not an error.
        if Domain not in self.domains:
            return {"DomainDescription": {}}
        return {"DomainDescription": {"Domain": Domain, "UserPoolId": self.domains[Domain]}}

    def create_user_pool_domain(self, Domain, UserPoolId):  # noqa: N803
        self.domains[Domain] = UserPoolId
        return {"CloudFrontDomain": "d123.cloudfront.net"}


class FakeControl:
    """bedrock-agentcore-control: gateways, memories, OAuth providers."""

    def __init__(self) -> None:
        self.gateways: dict[str, dict] = {}
        self.memories: dict[str, dict] = {}
        self.providers: dict[str, str] = {}
        self.created_gateways: list[str] = []
        self.created_memories: list[str] = []
        self.created_providers: list[str] = []
        self.updated: list[str] = []

    def preexisting_gateway(self, name: str) -> str:
        gateway_id = f"{name}-{len(self.gateways) + 1}"
        self.gateways[gateway_id] = {
            "gatewayId": gateway_id,
            "name": name,
            "gatewayArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{gateway_id}",
            "gatewayUrl": f"https://{gateway_id}.gateway.example/mcp",
            "status": "READY",
            "roleArn": f"arn:aws:iam::{ACCOUNT}:role/launchpad-gateway-role-{REGION}",
            "protocolType": "MCP",
            "authorizerType": "CUSTOM_JWT",
            "authorizerConfiguration": {
                "customJWTAuthorizer": {"discoveryUrl": "https://x", "allowedClients": []}
            },
        }
        return gateway_id

    def list_gateways(self, maxResults=None, nextToken=None):  # noqa: N803
        return {"items": list(self.gateways.values())}

    def get_gateway(self, gatewayIdentifier):  # noqa: N803
        return self.gateways[gatewayIdentifier]

    def create_gateway(self, name, **kwargs):
        gateway_id = self.preexisting_gateway(name)
        self.gateways[gateway_id]["roleArn"] = kwargs.get("roleArn", "")
        self.gateways[gateway_id]["authorizerConfiguration"] = kwargs.get(
            "authorizerConfiguration", {}
        )
        self.created_gateways.append(name)
        return {"gatewayId": gateway_id}

    def update_gateway(self, gatewayIdentifier, **kwargs):  # noqa: N803
        self.gateways[gatewayIdentifier].update(kwargs)
        self.updated.append(gatewayIdentifier)
        return {}

    def preexisting_memory(self, name: str) -> str:
        memory_id = f"{name}-abc{len(self.memories) + 1}"
        self.memories[memory_id] = {
            "id": memory_id,
            "arn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:memory/{memory_id}",
            "status": "ACTIVE",
        }
        return memory_id

    def list_memories(self, maxResults=None, nextToken=None):  # noqa: N803
        return {"memories": list(self.memories.values())}

    def create_memory(self, name, **kwargs):
        memory_id = self.preexisting_memory(name)
        self.created_memories.append(name)
        return {"memory": self.memories[memory_id]}

    def get_memory(self, memoryId):  # noqa: N803
        return {"memory": self.memories[memoryId]}

    def list_oauth2_credential_providers(self, maxResults=None):  # noqa: N803
        return {
            "credentialProviders": [
                {"name": name, "credentialProviderArn": arn}
                for name, arn in self.providers.items()
            ]
        }

    def create_oauth2_credential_provider(self, name, **kwargs):
        arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:token-vault/default/{name}"
        self.providers[name] = arn
        self.created_providers.append(name)
        return {"credentialProviderArn": arn}


class FakeRegistry:
    def __init__(self) -> None:
        self.registries: dict[str, dict] = {}
        self.created: list[str] = []
        self.denied = False

    def preexisting(self, name: str) -> None:
        registry_id = f"{name}-xyz"
        self.registries[registry_id] = {
            "name": name,
            "registryId": registry_id,
            "registryArn": f"arn:aws:agent-registry:{REGION}:{ACCOUNT}:registry/{registry_id}",
        }

    def list_registries(self, maxResults=None, nextToken=None):  # noqa: N803
        if self.denied:
            raise _client_error("AccessDeniedException", "ListRegistries")
        return {"registries": list(self.registries.values())}

    def create_registry(self, name, description=None):
        self.preexisting(name)
        self.created.append(name)
        registry = next(r for r in self.registries.values() if r["name"] == name)
        return {"registryArn": registry["registryArn"]}


class FakeXray:
    """Transaction Search is a per-region setting, so it starts off."""

    def __init__(self) -> None:
        self.destination = "XRay"
        self.updates: list[str] = []

    def get_trace_segment_destination(self):
        status = "ACTIVE" if self.destination == "CloudWatchLogs" else "PENDING"
        return {"Destination": self.destination, "Status": status}

    def update_trace_segment_destination(self, Destination):  # noqa: N803
        self.destination = Destination
        self.updates.append(Destination)
        return {}


class FakeAws:
    def __init__(self) -> None:
        self.sts = FakeSts()
        self.iam = FakeIam()
        self.s3 = FakeS3()
        self.ecr = FakeEcr()
        self.codebuild = FakeCodeBuild()
        self.cognito = FakeCognito()
        self.control = FakeControl()
        self.registry = FakeRegistry()
        self.xray = FakeXray()
        self.expect_region = REGION
        self.services = {
            "sts": self.sts,
            "iam": self.iam,
            "s3": self.s3,
            "ecr": self.ecr,
            "codebuild": self.codebuild,
            "cognito-idp": self.cognito,
            "bedrock-agentcore-control": self.control,
            "agent-registry-control": self.registry,
            "xray": self.xray,
        }

    def client(self, service, ctx, cache_token=None, **cfg):
        assert ctx.region == self.expect_region, (
            f"client built for {ctx.region}, not the workspace's region"
        )
        if service not in self.services:
            raise AssertionError(f"a stage asked for an unexpected client: {service}")
        return self.services[service]


@pytest.fixture
def aws(monkeypatch) -> FakeAws:
    fake = FakeAws()
    monkeypatch.setattr(aws_clients, "client", fake.client)
    return fake


def _fast() -> wb.Timeouts:
    return wb.Timeouts(
        memory_s=1, gateway_s=1, iam_attempts=2, iam_delay_s=0, sleeper=lambda _s: None
    )


def _register(
    workspace_id: str = WS_ID,
    *,
    bootstrap_status: str = "registered",
    resources: dict | None = None,
    region: str = REGION,
) -> None:
    db = SessionLocal()
    try:
        db.add(
            Workspace(
                id=workspace_id,
                name=workspace_id,
                account_id=ACCOUNT,
                region=region,
                bootstrap_status=bootstrap_status,
                resources=dict(resources or {}),
            )
        )
        db.commit()
    finally:
        db.close()


def _ctx(workspace_id: str = WS_ID) -> wb.BootstrapContext:
    return wb.BootstrapContext(
        workspace_id=workspace_id,
        job_id="ctx-only",
        workspace=context_for_workspace(workspace_id),
        timeouts=_fast(),
    )


def _row(workspace_id: str = WS_ID) -> Workspace:
    db = SessionLocal()
    try:
        row = db.get(Workspace, workspace_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def _job_row(job_id: str) -> Job:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        db.expunge(job)
        return job
    finally:
        db.close()


def _queue_job(workspace_id: str = WS_ID) -> str:
    db = SessionLocal()
    try:
        job = wb.create_bootstrap_job(db, db.get(Workspace, workspace_id))
        return job.id
    finally:
        db.close()


def _stage_status(job: Job) -> dict[str, str]:
    return {entry["name"]: entry["status"] for entry in wb.job_stages(job)}


# ── stages ─────────────────────────────────────────────────────────────────


class TestStages:
    def test_a_full_pass_provisions_the_documented_resource_map(self, aws):
        _register()
        ctx = _ctx()
        for name in wb.STAGE_ORDER:
            wb.STAGES[name](ctx)

        resources = _row().resources
        assert set(resources) == set(wb.REQUIRED_RESOURCE_KEYS) | {
            "registry_id",
            "registry_arn",
            "registry_unavailable_reason",
            "user_pool_domain",
        }
        assert resources["artifacts_bucket"] == f"launchpad-artifacts-{ACCOUNT}-{REGION}"
        assert resources["ecr_repo"] == "launchpad-agents"
        assert resources["codebuild_project"] == "launchpad-agent-builder"
        assert resources["execution_role_arn"].endswith(
            f"role/launchpad-agent-execution-role-{REGION}"
        )
        assert resources["user_pool_domain"] == f"launchpad-{ACCOUNT}-{REGION}"
        assert _row().bootstrap_status == "ready"

    def test_every_stage_is_idempotent(self, aws):
        """Second pass over the same environment must create nothing new — this is
        what makes a resumed job safe."""
        _register()
        ctx = _ctx()
        for name in wb.STAGE_ORDER:
            wb.STAGES[name](ctx)
        first = _row().resources

        for name in wb.STAGE_ORDER:
            wb.STAGES[name](ctx)

        assert _row().resources == first
        assert aws.iam.created == [
            f"launchpad-agent-execution-role-{REGION}",
            f"launchpad-gateway-role-{REGION}",
            f"launchpad-kb-role-{REGION}",
            f"launchpad-codebuild-role-{REGION}",
        ]
        assert len(aws.s3.created) == 1
        assert aws.ecr.created == ["launchpad-agents"]
        assert aws.codebuild.created == ["launchpad-agent-builder"]
        assert aws.cognito.created_pools == ["launchpad-users"]
        assert aws.cognito.created_clients == ["launchpad-console", "launchpad-agent-m2m"]
        assert aws.control.created_gateways == ["launchpad-gw"]
        assert aws.control.created_memories == ["launchpad_memory"]
        assert aws.control.created_providers == ["launchpad-gw-m2m"]
        assert aws.registry.created == ["launchpad-registry"]

    def test_the_bucket_carries_the_cdk_posture(self, aws):
        _register()
        wb.STAGES["storage"](_ctx())
        bucket = f"launchpad-artifacts-{ACCOUNT}-{REGION}"
        applied = aws.s3.settings[bucket]
        assert aws.s3.created[0]["location"] == REGION
        assert applied["versioning"] == {"Status": "Enabled"}
        assert applied["public_access_block"]["BlockPublicAcls"] is True
        assert "aws:SecureTransport" in applied["policy"]

    def test_us_east_1_gets_no_location_constraint(self, aws):
        """create_bucket rejects a LocationConstraint naming us-east-1."""
        aws.expect_region = "us-east-1"
        _register(region="us-east-1")

        wb.STAGES["storage"](_ctx())

        assert aws.s3.created[0]["location"] is None

    def test_the_gateway_stage_creates_no_demo_targets(self, aws):
        _register()
        ctx = _ctx()
        for name in ("iam", "storage", "cognito"):
            wb.STAGES[name](ctx)
        detail = wb.STAGES["gateway"](ctx)

        assert "no demo targets" in detail
        # the m2m client is allowed on the gateway, and nothing else touched it
        assert aws.control.updated == [_row().resources["gateway_id"]]
        allowed = aws.control.gateways[_row().resources["gateway_id"]][
            "authorizerConfiguration"
        ]["customJWTAuthorizer"]["allowedClients"]
        assert allowed == ["cid-launchpad-console", "cid-launchpad-agent-m2m"]

    def test_registry_denial_is_tolerated_like_the_hub(self, aws):
        _register()
        aws.registry.denied = True
        detail = wb.STAGES["registry"](_ctx())

        assert "unavailable" in detail
        resources = _row().resources
        assert resources["registry_id"] == ""
        assert resources["registry_unavailable_reason"]

    def test_observability_enables_transaction_search_in_the_new_region(self, aws):
        """Every trace/session/token view reads the `aws/spans` group this fills,
        and it is a per-region setting the hub's own bootstrap also turns on."""
        _register()
        detail = wb.STAGES["observability"](_ctx())

        assert aws.xray.updates == ["CloudWatchLogs"]
        assert "active" in detail
        # already-on is a no-op, not a second update
        assert "already" in wb.STAGES["observability"](_ctx())
        assert aws.xray.updates == ["CloudWatchLogs"]

    def test_observability_degrades_instead_of_failing_the_workspace(
        self, aws, monkeypatch
    ):
        _register()
        monkeypatch.setattr(
            aws.xray,
            "update_trace_segment_destination",
            lambda **_kw: (_ for _ in ()).throw(
                _client_error("AccessDeniedException", "UpdateTraceSegmentDestination")
            ),
        )
        detail = wb.STAGES["observability"](_ctx())

        assert detail == "unavailable · AccessDeniedException"

    def test_finalize_refuses_an_incomplete_resource_map(self, aws):
        _register(resources={"memory_id": "launchpad_memory-1"})
        with pytest.raises(wb.BootstrapError) as exc:
            wb.STAGES["finalize"](_ctx())
        assert "missing" in str(exc.value)
        assert _row().bootstrap_status == "registered"


class TestRolePolicyFidelity:
    """The roles are a transcription of `infra/stacks/base_stack.py`, and the
    workspace's own account/region are the only ones they may name. A wrong
    literal here is invisible until an agent 401s at invoke time on real AWS.
    """

    # base_stack.py:201-400 (exec_role.add_to_policy calls, in order)
    EXECUTION_SIDS = (
        "BedrockModels",
        "BedrockMantleInference",
        "BedrockMantleCallWithBearerToken",
        "MarketplaceOperationsFromBedrockMantleFor3pModels",
        "AgentCoreDataPlane",
        "EcrPull",
        "EcrAuth",
        "SkillBundleObjects",
        "SkillBundleList",
        "ABTestOrchestration",
        "ManagedKbRetrieval",
        "ManagedKbAgenticRetrieval",
        "IdentityVaultSecrets",
        "Telemetry",
    )
    # base_stack.py:459-555, minus the two demo-target statements a boto3
    # bootstrap cannot deploy (hr_lambda.grant_invoke, InvokeRestTargets).
    GATEWAY_SIDS = (
        "IdentityVault",
        "InvokeRuntimeTargets",
        "ConfigurationBundleRead",
        "PolicyEngineEvaluation",
        "IdentitySecrets",
        "ManagedKbRetrieval",
        "ManagedKbAgenticRetrieval",
    )

    def _documents(self) -> dict[str, dict]:
        bucket = f"launchpad-artifacts-{ACCOUNT}-{REGION}"
        repo_arn = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/launchpad-agents"
        return {
            "execution": workspace_iam.execution_role_policy(
                ACCOUNT, REGION, bucket, repo_arn
            ),
            "gateway": workspace_iam.gateway_role_policy(ACCOUNT, REGION),
            "kb": workspace_iam.kb_role_policy(bucket),
            "codebuild": workspace_iam.codebuild_role_policy(
                ACCOUNT, REGION, bucket, repo_arn, "launchpad-agent-builder"
            ),
        }

    def test_the_execution_role_carries_every_cdk_statement(self):
        document = self._documents()["execution"]
        sids = tuple(s["Sid"] for s in document["Statement"])
        assert sids == self.EXECUTION_SIDS

    def test_the_gateway_role_drops_only_the_demo_target_statements(self):
        document = self._documents()["gateway"]
        sids = tuple(s["Sid"] for s in document["Statement"])
        assert sids == self.GATEWAY_SIDS
        actions = {a for s in document["Statement"] for a in _actions(s)}
        assert "lambda:InvokeFunction" not in actions
        assert "execute-api:Invoke" not in actions

    def test_the_kb_role_covers_the_buckets_kb_prefix_only(self):
        statements = self._documents()["kb"]["Statement"]
        assert tuple(s["Sid"] for s in statements) == ("KbDataObjects", "KbDataList")
        assert statements[0]["Resource"].endswith("/kb/*")
        assert statements[1]["Condition"] == {"StringLike": {"s3:prefix": "kb/*"}}

    def test_every_arn_names_this_workspaces_account_and_region(self):
        """The failure this catches: an ARN built from hub settings instead of the
        workspace context, which grants nothing in the new region (or something in
        the wrong one)."""
        for role, document in self._documents().items():
            for statement in document["Statement"]:
                resources = statement["Resource"]
                for arn in [resources] if isinstance(resources, str) else resources:
                    if not arn.startswith("arn:"):
                        continue
                    _, _, _, region, account, *_ = arn.split(":")
                    # "*" only where the CDK wildcards it too: Mantle models are
                    # hosted outside the workspace's region.
                    assert region in (REGION, "*", ""), f"{role}/{statement['Sid']}: {arn}"
                    assert account in (ACCOUNT, ""), f"{role}/{statement['Sid']}: {arn}"

    def test_the_trust_policies_pin_the_service_and_the_account(self):
        trust = workspace_iam.service_trust_policy(
            "bedrock-agentcore.amazonaws.com", ACCOUNT
        )
        statement = trust["Statement"][0]
        assert statement["Principal"] == {"Service": "bedrock-agentcore.amazonaws.com"}
        assert statement["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}
        # CodeBuild's CDK-generated trust has no SourceAccount condition, and a
        # role a build cannot assume fails at package time, not at bootstrap.
        build_trust = workspace_iam.service_trust_policy(
            "codebuild.amazonaws.com", ACCOUNT, source_account=False
        )
        assert "Condition" not in build_trust["Statement"][0]

    def test_the_stage_puts_exactly_these_documents_on_the_roles(self, aws):
        _register()
        wb.STAGES["iam"](_ctx())
        wb.STAGES["storage"](_ctx())
        wb.STAGES["codebuild"](_ctx())
        expected = self._documents()
        assert json.loads(
            aws.iam.inline[
                (f"launchpad-agent-execution-role-{REGION}", "launchpad-agent-execution")
            ]
        ) == expected["execution"]
        assert json.loads(
            aws.iam.inline[(f"launchpad-gateway-role-{REGION}", "launchpad-gateway")]
        ) == expected["gateway"]
        assert json.loads(
            aws.iam.inline[(f"launchpad-kb-role-{REGION}", "launchpad-kb")]
        ) == expected["kb"]
        assert json.loads(
            aws.iam.inline[
                (f"launchpad-codebuild-role-{REGION}", "launchpad-agent-builder")
            ]
        ) == expected["codebuild"]


class TestValidateAccess:
    def test_an_account_mismatch_fails_with_both_ids(self, aws):
        _register()
        aws.sts.account = "999988887777"
        with pytest.raises(wb.BootstrapError) as exc:
            wb.STAGES["validate-access"](_ctx())
        message = str(exc.value)
        assert "999988887777" in message and ACCOUNT in message

    def test_a_region_without_the_service_is_named_as_unsupported(self, aws, monkeypatch):
        _register()
        monkeypatch.setattr(
            aws.control,
            "list_gateways",
            lambda **_kw: (_ for _ in ()).throw(
                _client_error("UnrecognizedClientException", "ListGateways")
            ),
        )
        with pytest.raises(wb.BootstrapError) as exc:
            wb.STAGES["validate-access"](_ctx())
        message = str(exc.value)
        assert "bedrock-agentcore-control" in message and "region not supported" in message

    def test_an_existing_launchpad_gateway_is_refused_not_adopted(self, aws):
        """The cautionary case: this account runs an independent production
        install whose region-scoped names all match."""
        _register()
        aws.control.preexisting_gateway("launchpad-gw")
        with pytest.raises(wb.ForeignResourceError) as exc:
            wb.STAGES["validate-access"](_ctx())
        message = str(exc.value)
        assert "already hosts a Launchpad deployment" in message
        assert "gateway launchpad-gw" in message

    @pytest.mark.parametrize(
        ("seed", "expected"),
        [
            ("memory", "memory launchpad_memory-*"),
            ("registry", "registry launchpad-registry"),
            ("pool", "Cognito user pool launchpad-users"),
            ("project", "CodeBuild project launchpad-agent-builder"),
        ],
    )
    def test_every_marker_resource_triggers_the_refusal(self, aws, seed, expected):
        _register()
        if seed == "memory":
            aws.control.preexisting_memory("launchpad_memory")
        elif seed == "registry":
            aws.registry.preexisting("launchpad-registry")
        elif seed == "pool":
            aws.cognito.preexisting_pool("launchpad-users")
        else:
            aws.codebuild.projects["launchpad-agent-builder"] = {"name": "x"}

        with pytest.raises(wb.ForeignResourceError) as exc:
            wb.STAGES["validate-access"](_ctx())
        assert expected in str(exc.value)

    def test_resources_this_workspace_already_recorded_are_its_own(self, aws):
        """Which is what makes validate-access re-runnable after a partial run."""
        gateway_id = aws.control.preexisting_gateway("launchpad-gw")
        memory_id = aws.control.preexisting_memory("launchpad_memory")
        aws.registry.preexisting("launchpad-registry")
        pool_id = aws.cognito.preexisting_pool("launchpad-users")
        aws.codebuild.projects["launchpad-agent-builder"] = {"name": "x"}
        _register(
            resources={
                "gateway_id": gateway_id,
                "memory_id": memory_id,
                "registry_id": "launchpad-registry-xyz",
                "user_pool_id": pool_id,
                "codebuild_project": "launchpad-agent-builder",
            }
        )
        assert "usable" in wb.STAGES["validate-access"](_ctx())

    def test_a_registry_denial_does_not_block_the_refusal_check(self, aws):
        _register()
        aws.registry.denied = True
        assert "usable" in wb.STAGES["validate-access"](_ctx())


class TestIamAdoption:
    def test_an_untagged_role_of_the_same_name_is_refused(self, aws):
        _register()
        aws.iam.preexisting(f"launchpad-agent-execution-role-{REGION}")
        with pytest.raises(wb.ForeignResourceError) as exc:
            wb.STAGES["iam"](_ctx())
        assert "without a launchpad:workspace tag" in str(exc.value)

    def test_a_role_this_workspace_made_is_adopted(self, aws):
        _register()
        name = f"launchpad-agent-execution-role-{REGION}"
        aws.iam.preexisting(name, {"launchpad:workspace": WS_ID})
        wb.STAGES["iam"](_ctx())
        assert name not in aws.iam.created
        assert _row().resources["execution_role_arn"].endswith(name)

    def test_a_retired_workspaces_role_is_adopted_and_restamped(self, aws):
        """Only one workspace can exist per (account, region), so a tag naming
        another one means that workspace is gone."""
        _register()
        name = f"launchpad-gateway-role-{REGION}"
        aws.iam.preexisting(name, {"launchpad:workspace": "retired-ws"})
        wb.STAGES["iam"](_ctx())
        assert aws.iam.roles[name]["tags"]["launchpad:workspace"] == WS_ID
        assert name in aws.iam.retagged


# ── the runner ─────────────────────────────────────────────────────────────


class TestRunner:
    def test_a_clean_run_marks_every_stage_and_the_workspace_ready(self, aws):
        _register()
        job_id = _queue_job()
        assert _row().bootstrap_status == "bootstrapping"

        wb.execute_bootstrap_job(job_id, timeouts=_fast())

        job = _job_row(job_id)
        assert job.status == "succeeded"
        assert set(_stage_status(job).values()) == {"succeeded"}
        assert _row().bootstrap_status == "ready"
        # JSONL events, one per stage at least
        assert job.log.count("stage started") == len(wb.STAGE_ORDER)

    def test_a_failed_stage_fails_the_row_and_keeps_partial_resources(
        self, aws, monkeypatch
    ):
        _register()
        monkeypatch.setattr(
            aws.cognito,
            "create_user_pool",
            lambda **_kw: (_ for _ in ()).throw(RuntimeError("pool refused")),
        )
        job_id = _queue_job()

        wb.execute_bootstrap_job(job_id, timeouts=_fast())

        job = _job_row(job_id)
        assert job.status == "failed"
        assert "pool refused" in (job.error or "")
        assert _stage_status(job) == {
            "validate-access": "succeeded",
            "iam": "succeeded",
            "storage": "succeeded",
            "codebuild": "succeeded",
            "cognito": "failed",
            "gateway": "pending",
            "memory": "pending",
            "registry": "pending",
            "observability": "pending",
            "finalize": "pending",
        }
        row = _row()
        assert row.bootstrap_status == "failed"
        # what the succeeded stages made is still recorded — that is what turns
        # the retry into a resume
        assert row.resources["artifacts_bucket"]
        assert row.resources["ecr_repo"]
        assert row.resources["execution_role_arn"]
        assert "gateway_id" not in row.resources
        assert "memory_id" not in row.resources

    def test_resources_are_written_per_stage_not_per_job(self, aws, monkeypatch):
        """A job that dies right after `storage` must leave the bucket and repo on
        the row; anything later must be absent."""
        _register()
        monkeypatch.setitem(
            wb.STAGES,
            "codebuild",
            lambda _ctx: (_ for _ in ()).throw(RuntimeError("killed")),
        )
        job_id = _queue_job()

        wb.execute_bootstrap_job(job_id, timeouts=_fast())

        resources = _row().resources
        assert resources["artifacts_bucket"] == f"launchpad-artifacts-{ACCOUNT}-{REGION}"
        assert resources["ecr_repo_uri"].endswith("/launchpad-agents")
        assert "codebuild_project" not in resources
        assert "gateway_id" not in resources

    def test_a_retry_resumes_from_the_first_unfinished_stage(self, aws, monkeypatch):
        _register()
        # scoped, not undo(): the `aws` fixture patched the client factory through
        # the same monkeypatch, and undo() would revert that too
        with monkeypatch.context() as patched:
            patched.setattr(
                aws.cognito,
                "create_user_pool",
                lambda **_kw: (_ for _ in ()).throw(RuntimeError("pool refused")),
            )
            wb.execute_bootstrap_job(_queue_job(), timeouts=_fast())
        assert _row().bootstrap_status == "failed"
        roles_after_first_run = list(aws.iam.created)

        second = _queue_job()
        assert _stage_status(_job_row(second)) == {
            "validate-access": "succeeded",
            "iam": "succeeded",
            "storage": "succeeded",
            "codebuild": "succeeded",
            "cognito": "pending",
            "gateway": "pending",
            "memory": "pending",
            "registry": "pending",
            "observability": "pending",
            "finalize": "pending",
        }

        wb.execute_bootstrap_job(second, timeouts=_fast())

        assert _job_row(second).status == "succeeded"
        assert _row().bootstrap_status == "ready"
        # the skipped stages did not provision a second set of anything
        assert aws.iam.created == roles_after_first_run
        assert len(aws.s3.created) == 1
        assert aws.cognito.created_pools == ["launchpad-users"]

    def test_a_resumed_job_reads_the_previous_runs_identifiers(self, aws, monkeypatch):
        """The context is rebuilt from the row, so a stage after the interruption
        finds what earlier stages recorded rather than re-deriving it."""
        _register()
        with monkeypatch.context() as patched:
            patched.setitem(
                wb.STAGES,
                "cognito",
                lambda _ctx: (_ for _ in ()).throw(RuntimeError("stop")),
            )
            wb.execute_bootstrap_job(_queue_job(), timeouts=_fast())

        second = _queue_job()
        wb.execute_bootstrap_job(second, timeouts=_fast())

        # the gateway stage needs iam's + cognito's output; it only had the row
        assert _row().resources["gateway_id"] in aws.control.gateways
        assert aws.control.gateways[_row().resources["gateway_id"]]["roleArn"].endswith(
            f"launchpad-gateway-role-{REGION}"
        )

    def test_a_missing_workspace_row_fails_the_job_instead_of_crashing(self, aws):
        _register()
        job_id = _queue_job()
        db = SessionLocal()
        try:
            db.delete(db.get(Workspace, WS_ID))
            db.commit()
        finally:
            db.close()

        wb.execute_bootstrap_job(job_id, timeouts=_fast())

        job = _job_row(job_id)
        assert job.status == "failed"
        assert "no longer exists" in (job.error or "")

    def test_a_second_run_cannot_be_queued_while_one_owns_the_workspace(self, aws):
        """The claim is a conditional UPDATE, so the loser of a two-request race
        cannot queue a second run — two runs would race on every AWS create."""
        _register()
        first = _queue_job()

        with pytest.raises(wb.BootstrapConflict):
            _queue_job()

        db = SessionLocal()
        try:
            queued = db.query(Job).filter(Job.type == wb.JOB_TYPE).all()
        finally:
            db.close()
        assert [job.id for job in queued] == [first]

    def test_resume_pending_jobs_restarts_an_interrupted_bootstrap(self, monkeypatch):
        _register()
        job_id = _queue_job()
        started: list[str] = []
        monkeypatch.setattr(wb, "start_bootstrap_async", lambda job: started.append(job))

        resumed = pipeline.resume_pending_jobs()

        assert started == [job_id]
        assert resumed == [job_id]


class TestResourceMergeLock:
    def test_concurrent_merges_from_two_threads_keep_every_key(self):
        """The bootstrap job writes `resources` per stage on its own thread while a
        request can lazily provision the KB gateway — an unlocked
        read-modify-write of the JSON column would drop one side's keys."""
        _register()
        writers = 2
        per_writer = 25
        errors: list[Exception] = []
        start = threading.Barrier(writers)

        def merge(prefix: str) -> None:
            try:
                workspace = context_for_workspace(WS_ID)
                start.wait(timeout=5)
                for index in range(per_writer):
                    merge_workspace_resources(workspace, {f"{prefix}{index}": index})
            except Exception as exc:  # noqa: BLE001 — re-raised in the assertion
                errors.append(exc)

        threads = [
            threading.Thread(target=merge, args=(prefix,)) for prefix in ("job-", "req-")
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, errors
        resources = _row().resources
        assert len(resources) == writers * per_writer
        assert resources["job-24"] == 24 and resources["req-24"] == 24


# ── the admin route ────────────────────────────────────────────────────────


@pytest.fixture
def no_thread(monkeypatch) -> list[str]:
    """Keep the route tests off the worker thread: the job itself is exercised
    synchronously above."""
    started: list[str] = []
    monkeypatch.setattr(
        "app.services.workspace_bootstrap.start_bootstrap_async",
        lambda job_id: started.append(job_id),
    )
    return started


class TestBootstrapRoute:
    def test_it_queues_a_job_and_reports_its_stages(self, client, no_thread):
        _register()
        response = client.post(f"/api/workspaces/{WS_ID}/bootstrap")

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["workspace_id"] == WS_ID
        assert [entry["name"] for entry in body["stages"]] == wb.STAGE_ORDER
        assert {entry["status"] for entry in body["stages"]} == {"pending"}
        assert no_thread == [body["job_id"]]

        job = _job_row(body["job_id"])
        assert job.type == wb.JOB_TYPE
        assert job.workspace_id == WS_ID
        assert job.payload["workspace_id"] == WS_ID
        assert _row().bootstrap_status == "bootstrapping"

    def test_the_job_is_pollable_through_the_jobs_route(self, client, no_thread):
        _register()
        job_id = client.post(f"/api/workspaces/{WS_ID}/bootstrap").json()["job_id"]

        polled = client.get(f"/api/jobs/{job_id}", headers={"X-Workspace": WS_ID})

        assert polled.status_code == 200, polled.text
        assert polled.json()["type"] == wb.JOB_TYPE
        assert [s["name"] for s in polled.json()["payload"]["stages"]] == wb.STAGE_ORDER

    def test_the_latest_run_is_discoverable_without_its_job_id(self, client, no_thread):
        """A browser that did not start the run finds it via GET .../bootstrap."""
        _register()
        assert client.get(f"/api/workspaces/{WS_ID}/bootstrap").json()["job"] is None

        job_id = client.post(f"/api/workspaces/{WS_ID}/bootstrap").json()["job_id"]
        status = client.get(f"/api/workspaces/{WS_ID}/bootstrap")

        assert status.status_code == 200, status.text
        body = status.json()
        assert body["workspace_id"] == WS_ID
        assert body["bootstrap_status"] == "bootstrapping"
        assert body["job"]["id"] == job_id
        assert [s["name"] for s in body["job"]["stages"]] == wb.STAGE_ORDER

    def test_the_latest_run_route_404s_an_unknown_workspace(self, client, no_thread):
        assert client.get("/api/workspaces/nope/bootstrap").status_code == 404

    @pytest.mark.parametrize("status", ["bootstrapping", "ready"])
    def test_it_refuses_a_workspace_that_is_running_or_done(
        self, client, no_thread, status
    ):
        _register(bootstrap_status=status)
        response = client.post(f"/api/workspaces/{WS_ID}/bootstrap")

        assert response.status_code == 409
        assert response.json()["code"] == "workspace.bootstrap_conflict"
        assert no_thread == []

    def test_a_failed_workspace_may_be_retried(self, client, no_thread):
        _register(bootstrap_status="failed")
        assert client.post(f"/api/workspaces/{WS_ID}/bootstrap").status_code == 202

    def test_the_default_workspace_belongs_to_make_bootstrap(self, client, no_thread):
        response = client.post(f"/api/workspaces/{DEFAULT_WORKSPACE_ID}/bootstrap")

        assert response.status_code == 400
        assert response.json()["code"] == "workspace.default_not_bootstrappable"
        assert no_thread == []

    def test_losing_the_claim_race_answers_409_not_500(
        self, client, no_thread, monkeypatch
    ):
        """The router's status check can be overtaken by a concurrent request; the
        service's claim is what decides, and its refusal is the same 409."""
        _register()
        monkeypatch.setattr(
            wb,
            "create_bootstrap_job",
            lambda _db, row: (_ for _ in ()).throw(wb.BootstrapConflict(row.id)),
        )
        response = client.post(f"/api/workspaces/{WS_ID}/bootstrap")

        assert response.status_code == 409
        assert response.json()["code"] == "workspace.bootstrap_conflict"
        assert no_thread == []

    def test_an_unknown_workspace_is_a_404(self, client, no_thread):
        response = client.post("/api/workspaces/nope/bootstrap")

        assert response.status_code == 404
        assert response.json()["code"] == "workspace.not_found"


class TestBootstrapRouteIsAdminOnly:
    @pytest.fixture
    def gated_app(self, monkeypatch):
        monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
        monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
        get_settings.cache_clear()
        yield create_app()
        get_settings.cache_clear()

    def test_a_member_cannot_bootstrap_a_workspace(self, gated_app, no_thread):
        _register()
        with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
            assert client.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
            db = SessionLocal()
            try:
                user = users_service.find_by_username(db, MEMBER_CREDS["username"])
                user.status = users_service.STATUS_ACTIVE
                user.expires_at = datetime.now(UTC) + timedelta(days=7)
                users_service.set_workspace_grants(db, user, [WS_ID])
                db.commit()
            finally:
                db.close()
            login = client.post(
                "/api/auth/login",
                json={
                    "username": MEMBER_CREDS["username"],
                    "password": MEMBER_CREDS["password"],
                },
            )
            assert login.status_code == 200, login.text

            response = client.post(f"/api/workspaces/{WS_ID}/bootstrap")

        assert response.status_code == 403
        assert no_thread == []
        assert _row().bootstrap_status == "registered"

    def test_an_admin_can(self, gated_app, no_thread):
        _register()
        with TestClient(gated_app, client=("127.0.0.1", 4321)) as client:
            assert client.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
            assert client.post(f"/api/workspaces/{WS_ID}/bootstrap").status_code == 202
