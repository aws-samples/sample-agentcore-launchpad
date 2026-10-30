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
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    NoRegionError,
    ReadTimeoutError,
    UnknownServiceError,
)
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import pipeline
from app.main import create_app
from app.models.ledger import Job, Workspace
from app.services import aws_clients, workspace_iam
from app.services import memory_bootstrap as mb
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

    def create_role(self, RoleName, AssumeRolePolicyDocument, Description, Tags):  # noqa: N803
        if RoleName in self.roles:
            raise _client_error("EntityAlreadyExists", "CreateRole")
        self.preexisting(RoleName, {tag["Key"]: tag["Value"] for tag in Tags})
        self.roles[RoleName]["trust"] = AssumeRolePolicyDocument
        self.created.append(RoleName)
        return {"Role": {"Arn": self.roles[RoleName]["arn"]}}

    # Like the real GetRole, the response carries Tags only when the role has some
    # (the untagged CDK-made role answers without the key — the ListRoleTags fallback
    # is what then decides); the trust document comes back URL-decoded as a dict.
    def get_role(self, RoleName):  # noqa: N803
        if RoleName not in self.roles:
            raise _client_error("NoSuchEntity", "GetRole")
        role = {"Arn": self.roles[RoleName]["arn"]}
        if self.roles[RoleName]["tags"]:
            role["Tags"] = [{"Key": k, "Value": v} for k, v in self.roles[RoleName]["tags"].items()]
        try:
            role["AssumeRolePolicyDocument"] = json.loads(self.roles[RoleName]["trust"])
        except (TypeError, ValueError):
            pass
        return {"Role": role}

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
        self.trust_updates = getattr(self, "trust_updates", []) + [RoleName]

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

    def batch_get_projects(self, names):
        return {
            "projects": [self.projects[n] for n in names if n in self.projects],
            "projectsNotFound": [n for n in names if n not in self.projects],
        }

    def create_project(self, **kwargs):
        self.projects[kwargs["name"]] = kwargs
        self.created.append(kwargs["name"])
        return {"project": kwargs}

    def update_project(self, **kwargs):
        self.projects[kwargs["name"]].update(kwargs)
        self.updated = getattr(self, "updated", [])
        self.updated.append(kwargs["name"])


class FakeControl:
    """bedrock-agentcore-control: gateways, memories, api-key credential providers."""

    def __init__(self) -> None:
        self.gateways: dict[str, dict] = {}
        self.memories: dict[str, dict] = {}
        self.created_gateways: list[str] = []
        self.updated_memories: list[tuple[str, list[str]]] = []
        self.created_memories: list[str] = []
        self.updated: list[str] = []
        self.providers: dict[str, dict] = {}
        self.provider_calls: list[str] = []

    def _provider(self, name: str) -> dict:
        return {
            "name": name,
            "credentialProviderArn": (
                f"arn:aws-cn:bedrock-agentcore:{REGION}:{ACCOUNT}:token-vault/default/"
                f"apikeycredentialprovider/{name}"
            ),
            "apiKeySecretArn": (
                f"arn:aws-cn:secretsmanager:{REGION}:{ACCOUNT}:secret:"
                f"bedrock-agentcore-identity!default/apikey/{name}-AbCdEf"
            ),
        }

    def get_api_key_credential_provider(self, name):
        self.provider_calls.append(f"get:{name}")
        if name not in self.providers:
            raise _client_error("ResourceNotFoundException", "GetApiKeyCredentialProvider")
        return self.providers[name]

    def create_api_key_credential_provider(self, name, apiKey):  # noqa: N803
        self.provider_calls.append(f"create:{name}")
        self.providers[name] = self._provider(name)
        return self.providers[name]

    def update_api_key_credential_provider(self, name, apiKey):  # noqa: N803
        self.provider_calls.append(f"update:{name}")
        return self.providers[name]

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
        self.gateways[gateway_id]["authorizerType"] = kwargs.get("authorizerType", "")
        self.gateways[gateway_id].pop("authorizerConfiguration", None)
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

    @staticmethod
    def _strategy_records(entries):
        # GetMemory reports each strategy by `type`/`name`, not by its input key
        from app.services.memory_strategies import STRATEGY_TYPES, strategy_kind

        return [
            {
                "strategyId": f"{entry[strategy_kind(entry)]['name']}-x",
                "name": entry[strategy_kind(entry)]["name"],
                "type": STRATEGY_TYPES[strategy_kind(entry)],
                "status": "ACTIVE",
            }
            for entry in entries
        ]

    def create_memory(self, name, **kwargs):
        memory_id = self.preexisting_memory(name)
        self.memories[memory_id]["strategies"] = self._strategy_records(
            kwargs.get("memoryStrategies") or []
        )
        self.created_memories.append(name)
        return {"memory": self.memories[memory_id]}

    def update_memory(self, memoryId, **kwargs):  # noqa: N803
        # additive — `addMemoryStrategies` is the only modification the platform sends
        added = (kwargs.get("memoryStrategies") or {}).get("addMemoryStrategies") or []
        memory = self.memories[memoryId]
        memory["strategies"] = list(memory.get("strategies") or []) + self._strategy_records(added)
        self.updated_memories.append((memoryId, [next(iter(e)) for e in added]))
        return {"memory": memory}

    def get_memory(self, memoryId):  # noqa: N803
        return {"memory": self.memories[memoryId]}


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


class FakeLogs:
    """A fresh region has no CloudWatch Logs resource policies at all — and no log
    groups (the D20 `policy` stage creates the decisions group)."""

    def __init__(self) -> None:
        self.policies: dict[str, str] = {}
        self.groups: dict[str, int | None] = {}
        self.created: list[str] = []

    def create_log_group(self, logGroupName):  # noqa: N803
        if logGroupName in self.groups:
            raise ClientError(
                {"Error": {"Code": "ResourceAlreadyExistsException", "Message": "exists"}},
                "CreateLogGroup",
            )
        self.groups[logGroupName] = None
        self.created.append(logGroupName)

    def put_retention_policy(self, logGroupName, retentionInDays):  # noqa: N803
        if logGroupName not in self.groups:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "no group"}},
                "PutRetentionPolicy",
            )
        self.groups[logGroupName] = retentionInDays

    def describe_resource_policies(self):
        return {
            "resourcePolicies": [
                {"policyName": name, "policyDocument": doc}
                for name, doc in self.policies.items()
            ]
        }

    def put_resource_policy(self, policyName, policyDocument):  # noqa: N803
        self.policies[policyName] = policyDocument
        return {}


class FakeSsm:
    """`ssm` for the D21 (M1-b) registry pass of the model-credential stage: no
    `launchpad-*-api-key` parameter exists unless a test stores one as (value, version)."""

    def __init__(self) -> None:
        self.parameters: dict[str, tuple[str, int]] = {}
        self.calls: list[str] = []

    def get_parameter(self, Name, WithDecryption=False):  # noqa: N803
        self.calls.append(Name)
        assert WithDecryption is True
        if Name not in self.parameters:
            raise _client_error("ParameterNotFound", "GetParameter")
        value, version = self.parameters[Name]
        return {"Parameter": {"Name": Name, "Value": value, "Version": version}}


class FakeAws:
    def __init__(self) -> None:
        self.sts = FakeSts()
        self.iam = FakeIam()
        self.s3 = FakeS3()
        self.ecr = FakeEcr()
        self.codebuild = FakeCodeBuild()
        self.control = FakeControl()
        self.registry = FakeRegistry()
        self.xray = FakeXray()
        self.logs = FakeLogs()
        self.ssm = FakeSsm()
        self.expect_region = REGION
        self.services = {
            "sts": self.sts,
            "iam": self.iam,
            "s3": self.s3,
            "ecr": self.ecr,
            "codebuild": self.codebuild,
            "bedrock-agentcore-control": self.control,
            "agent-registry-control": self.registry,
            "xray": self.xray,
            "logs": self.logs,
            "ssm": self.ssm,  # D21 M1-b: the model-credential stage's registry pass
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


@pytest.fixture(autouse=True)
def global_bedrock_key(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_GLOBAL_BEDROCK_API_KEY", "global-bedrock-key")


def _fast() -> wb.Timeouts:
    return wb.Timeouts(
        gateway_s=1, iam_attempts=2, iam_delay_s=0, sleeper=lambda _s: None
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


@pytest.fixture(autouse=True)
def _stub_skill_lab_stage(monkeypatch):
    """This suite asserts exact fake-AWS creation lists and resource maps; the
    skill-lab stage's internals (CodeBuild image, runtime, role) have their own
    focused fakes in test_skill_lab_foundation.py, so here the stage is a no-op
    that records nothing."""
    monkeypatch.setitem(wb.STAGES, "skill-lab", lambda _ctx: "stubbed")


@pytest.fixture(autouse=True)
def default_records(monkeypatch) -> list:
    """The registry stage's default-record import (N10-bug1) speaks MCP to the gateway
    and uploads the sample skill to S3 through the console code (its own fakes live in
    test_registry_attachables.py). Here it is a recorder returning no records unless a
    test replaces it; the stage's identifier bookkeeping is what this suite asserts."""
    from app.services import registry_console

    calls: list = []
    monkeypatch.setattr(registry_console, "ensure_default_records",
                        lambda ws, log=None, auto_approve=False: calls.append(ws) or [])
    return calls


# ── stages ─────────────────────────────────────────────────────────────────


class TestStages:
    def test_a_full_pass_provisions_the_documented_resource_map(self, aws):
        _register()
        ctx = _ctx()
        for name in wb.STAGE_ORDER:
            wb.STAGES[name](ctx)

        resources = _row().resources
        # model-credential records 2 extra keys (name, secret_arn) that are not
        # required for deploy/invoke (only the ARN is) but are still recorded; this
        # workspace is not the hub's own account+region, so memory degrades (N6 R2).
        assert set(resources) == set(wb.REQUIRED_RESOURCE_KEYS) | {
            "model_credential_name",
            "model_credential_secret_arn",
            "model_credentials",  # N-R6: per-provider map (name/arn/configured, no secret)
            "memory_unavailable_reason",
            # D20 P1-d: the CloudWatch decisions log group the audit sink mirrors into
            "policy_log_group",
            "policy_log_group_unavailable_reason",
        }
        assert resources["policy_log_group"] == "/launchpad/policy/decisions"
        assert aws.logs.groups == {"/launchpad/policy/decisions": 90}
        assert resources["artifacts_bucket"] == f"launchpad-artifacts-{ACCOUNT}-{REGION}"
        assert resources["ecr_repo"] == "launchpad-agents"
        assert resources["codebuild_project"] == "launchpad-agent-builder"
        assert resources["execution_role_arn"].endswith(
            f"role/launchpad-agent-execution-role-{REGION}"
        )
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
            f"launchpad-codebuild-role-{REGION}",
        ]
        assert len(aws.s3.created) == 1
        assert aws.ecr.created == ["launchpad-agents"]
        assert aws.codebuild.created == ["launchpad-agent-builder"]
        assert aws.control.created_gateways == ["launchpad-gw"]

    def test_the_bucket_carries_the_cdk_posture(self, aws):
        _register()
        wb.STAGES["storage"](_ctx())
        bucket = f"launchpad-artifacts-{ACCOUNT}-{REGION}"
        applied = aws.s3.settings[bucket]
        assert aws.s3.created[0]["location"] == REGION
        assert applied["versioning"] == {"Status": "Enabled"}
        assert applied["public_access_block"]["BlockPublicAcls"] is True
        assert "aws:SecureTransport" in applied["policy"]

    def test_codebuild_project_gets_the_cn_mirror_env_vars(self, aws):
        _register()
        wb.STAGES["storage"](_ctx())
        wb.STAGES["codebuild"](_ctx())
        env = {
            e["name"]: e["value"]
            for e in aws.codebuild.projects["launchpad-agent-builder"]["environment"][
                "environmentVariables"
            ]
        }
        # N11-r3-bug7: the project resolves from the BUILD mirror (NJU), never the host
        # mirror (tuna 4xx-blocks CodeBuild's egress), plus the fallback index.
        build_index = get_settings().codebuild_pip_index_url
        assert build_index != get_settings().pip_index_url
        assert env["PIP_INDEX_URL"] == build_index
        assert env["UV_INDEX_URL"] == build_index
        # uv 0.9.10 deprecates UV_INDEX_URL in favor of UV_DEFAULT_INDEX —
        # both must carry the mirror.
        assert env["UV_DEFAULT_INDEX"] == build_index
        assert env["PIP_FALLBACK_INDEX_URL"] == get_settings().codebuild_pip_fallback_index_url
        assert env["NPM_CONFIG_REGISTRY"] == get_settings().npm_registry
        assert "BASE_IMAGE" not in env  # disabled by default (R4)
        assert "PIP_EXTRA_INDEX_URL" not in env and "UV_INDEX" not in env  # N11-obs25 opt-in

    def test_codebuild_project_ignores_the_host_mirror_setting(self, aws, monkeypatch):
        """N11-r3-bug7: changing the host mirror must not move the build mirror."""
        settings = get_settings()
        monkeypatch.setattr(
            wb, "get_settings",
            lambda: settings.model_copy(update={
                "pip_index_url": "https://host-only.example.invalid/simple",
                "codebuild_pip_index_url": "https://build.example.invalid/simple",
                "codebuild_pip_fallback_index_url": "https://build-2.example.invalid/simple",
            }),
        )
        _register()
        wb.STAGES["storage"](_ctx())
        wb.STAGES["codebuild"](_ctx())
        env = {
            e["name"]: e["value"]
            for e in aws.codebuild.projects["launchpad-agent-builder"]["environment"][
                "environmentVariables"
            ]
        }
        assert env["PIP_INDEX_URL"] == "https://build.example.invalid/simple"
        assert env["UV_DEFAULT_INDEX"] == "https://build.example.invalid/simple"
        assert env["PIP_FALLBACK_INDEX_URL"] == "https://build-2.example.invalid/simple"
        assert "host-only" not in str(env)

    def test_codebuild_project_gets_the_optional_extra_index_when_configured(
        self, aws, monkeypatch
    ):
        settings = get_settings()
        monkeypatch.setattr(
            wb, "get_settings",
            lambda: settings.model_copy(
                update={"pip_extra_index_url": "https://mirror.nju.edu.cn/pypi/web/simple"}),
        )
        _register()
        wb.STAGES["storage"](_ctx())
        wb.STAGES["codebuild"](_ctx())
        env = {
            e["name"]: e["value"]
            for e in aws.codebuild.projects["launchpad-agent-builder"]["environment"][
                "environmentVariables"
            ]
        }
        assert env["PIP_EXTRA_INDEX_URL"] == "https://mirror.nju.edu.cn/pypi/web/simple"
        assert env["UV_INDEX"] == "https://mirror.nju.edu.cn/pypi/web/simple"
        assert env["PIP_INDEX_URL"] == settings.codebuild_pip_index_url  # N11-r3-bug7

    def test_us_east_1_gets_no_location_constraint(self, aws):
        """create_bucket rejects a LocationConstraint naming us-east-1."""
        aws.expect_region = "us-east-1"
        _register(region="us-east-1")

        wb.STAGES["storage"](_ctx())

        assert aws.s3.created[0]["location"] is None

    def test_the_gateway_stage_creates_no_demo_targets(self, aws):
        _register()
        ctx = _ctx()
        wb.STAGES["iam"](ctx)
        detail = wb.STAGES["gateway"](ctx)

        assert "no demo targets" in detail
        assert aws.control.created_gateways == ["launchpad-gw"]
        # no M2M allowed-clients list to maintain under IAM inbound auth
        assert aws.control.updated == []
        assert aws.control.gateways[_row().resources["gateway_id"]]["authorizerType"] == (
            "AWS_IAM"
        )

    def test_the_registry_stage_records_only_the_local_id(self, aws, default_records):
        _register()
        ctx = _ctx()
        detail = wb.STAGES["registry"](ctx)
        assert detail.startswith("local-")
        assert ctx.resources["registry_id"] == f"local-{ctx.workspace_id}"
        assert "registry_unavailable_reason" not in ctx.resources
        assert "registry_arn" not in ctx.resources
        assert wb.STAGE_ORDER.index("registry") == wb.STAGE_ORDER.index("gateway") + 1
        # N10-bug1: the stage ends with the default-record import on THIS workspace
        assert [ws.id for ws in default_records] == [ctx.workspace_id]
        assert detail.endswith("0 default record(s) · 0 new · 0 approved")

    def test_the_registry_stage_imports_the_defaults_and_counts_them(self, aws, monkeypatch):
        from app.services import registry_console

        _register()
        ctx = _ctx()
        lines: list[str] = []
        ctx.log = lines.append
        seen: dict = {}
        monkeypatch.setattr(
            registry_console, "ensure_default_records",
            lambda ws, log=None, auto_approve=False: seen.update(auto_approve=auto_approve) or [
            {"name": "hr-database", "type": "MCP", "record_id": "r1", "created": True,
             "status": "APPROVED"},
            {"name": "expense-report-writer", "type": "AGENT_SKILLS", "record_id": "r2",
             "created": True, "status": "PENDING_APPROVAL"},
            ],
        )

        detail = wb.STAGES["registry"](ctx)

        assert detail == (f"local-{ctx.workspace_id} · local ledger · 2 default record(s) · "
                          "2 new · 1 approved")
        assert seen == {"auto_approve": True}  # N17-obs257: the bootstrap job approves
        assert ctx.resources["registry_id"] == f"local-{ctx.workspace_id}"

    def test_the_registry_stage_degrades_when_the_import_fails(self, aws, monkeypatch):
        """No registry backend (503 feature.unavailable), a gateway that does not answer
        yet, S3 denied: the identifier is still recorded and the stage stays green with
        the reason — the records are one sync-defaults call away."""
        from app.core.errors import AppError
        from app.services import registry_console

        _register()
        ctx = _ctx()
        lines: list[str] = []
        ctx.log = lines.append

        def boom(ws, log=None, auto_approve=False):
            raise AppError("gateway.rpc_error", "tools/list refused", status_code=502)

        monkeypatch.setattr(registry_console, "ensure_default_records", boom)

        detail = wb.STAGES["registry"](ctx)

        assert detail == (f"local-{ctx.workspace_id} · local ledger · "
                          "defaults not imported (AppError)")
        assert ctx.resources["registry_id"] == f"local-{ctx.workspace_id}"
        assert any("POST /api/registry/sync-defaults" in line for line in lines)

    def test_observability_enables_transaction_search_in_the_new_region(self, aws):
        """Every trace/session/token view reads the `aws/spans` group this fills,
        and it is a per-region setting the hub's own bootstrap also turns on."""
        _register()
        detail = wb.STAGES["observability"](_ctx())

        assert aws.xray.updates == ["CloudWatchLogs"]
        assert "active" in detail
        # X-Ray cannot write aws/spans without a Logs RESOURCE policy — the
        # missing piece the live us-east-2 bootstrap diagnosed (2026-08-12).
        assert "TransactionSearchXRayAccess" in aws.logs.policies
        doc = aws.logs.policies["TransactionSearchXRayAccess"]
        assert f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:aws/spans:*" in doc
        # already-on is a no-op, not a second update and not a second policy put
        assert "already" in wb.STAGES["observability"](_ctx())
        assert aws.xray.updates == ["CloudWatchLogs"]
        assert len(aws.logs.policies) == 1

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


class TestModelCredentialStage:
    def test_is_ordered_right_after_iam_and_required(self):
        assert wb.STAGE_ORDER.index("model-credential") == wb.STAGE_ORDER.index("iam") + 1
        assert "model_credential_arn" in wb.REQUIRED_RESOURCE_KEYS

    @pytest.mark.siliconflow(registry=True)
    def test_creates_then_rotates(self, aws):
        _register()
        detail = wb.STAGES["model-credential"](_ctx())
        # N-R6: the optional SiliconFlow provider is reported, not created, without its key
        assert detail == ("launchpad-global-bedrock · created; "
                          "launchpad-siliconflow · not configured")
        assert aws.control.provider_calls == [
            "get:launchpad-global-bedrock",
            "create:launchpad-global-bedrock",
        ]
        resources = context_for_workspace(WS_ID).resources
        assert resources["model_credential_name"] == "launchpad-global-bedrock"
        assert resources["model_credential_arn"].endswith(
            "apikeycredentialprovider/launchpad-global-bedrock"
        )
        assert resources["model_credential_secret_arn"].startswith(
            f"arn:aws-cn:secretsmanager:{REGION}"
        )
        assert "global-bedrock-key" not in json.dumps(resources)

        detail = wb.STAGES["model-credential"](_ctx())
        assert detail == ("launchpad-global-bedrock · reused (key rotated); "
                          "launchpad-siliconflow · not configured")
        assert aws.control.provider_calls[-1] == "update:launchpad-global-bedrock"

    def test_missing_env_var_is_an_operator_error(self, aws, monkeypatch):
        monkeypatch.delenv("LAUNCHPAD_GLOBAL_BEDROCK_API_KEY")
        _register()
        with pytest.raises(wb.BootstrapError, match="LAUNCHPAD_GLOBAL_BEDROCK_API_KEY"):
            wb.STAGES["model-credential"](_ctx())
        assert aws.control.provider_calls == []


class TestMemoryStage:
    def test_is_ordered_after_registry_and_optional(self):
        assert wb.STAGE_ORDER.index("memory") == wb.STAGE_ORDER.index("registry") + 1
        assert "memory_id" not in wb.REQUIRED_RESOURCE_KEYS
        assert "kb_role_arn" not in wb.REQUIRED_RESOURCE_KEYS

    def test_a_foreign_workspace_degrades_with_a_reason(self, aws):
        _register()
        ctx = _ctx()
        detail = wb.STAGES["memory"](ctx)
        assert detail.startswith("unavailable")
        assert "hub" in _row().resources["memory_unavailable_reason"]
        assert "memory_id" not in _row().resources

    def test_a_hub_colocated_workspace_inherits_the_substrate(self, aws, monkeypatch):
        from tests.test_memory_bootstrap import OUTPUTS, SecOS

        settings = get_settings()
        fake = SecOS()
        monkeypatch.setattr(settings, "account_id", ACCOUNT, raising=False)
        monkeypatch.setattr(settings, "region", REGION, raising=False)
        memory_res = mb.memory_resources(
            OUTPUTS, bucket="hub-bkt", store_id="launchpad_memory-default"
        )
        monkeypatch.setattr(
            settings, "resources", {**settings.resources, **memory_res}, raising=False
        )
        monkeypatch.setattr(wb.aws_clients, "opensearch_http", lambda ctx, endpoint: fake)
        _register(resources={"execution_role_arn": f"arn:aws-cn:iam::{ACCOUNT}:role/exec"})
        ctx = _ctx()
        detail = wb.STAGES["memory"](ctx)
        res = _row().resources
        assert res["memory_id"] == f"launchpad_memory-{WS_ID}"
        assert res["memory_os_endpoint"] == OUTPUTS["MemoryDomainEndpoint"]
        assert res["memory_unavailable_reason"] == ""
        assert "created" in detail or "reused" in detail
        assert (
            fake.security["/_plugins/_security/api/rolesmapping/launchpad_memory_rw"]["users"][0]
            == f"arn:aws-cn:iam::{ACCOUNT}:role/exec"
        )


class TestRolePolicyFidelity:
    """The roles are a transcription of `infra/stacks/base_stack.py`, and the
    workspace's own account/region are the only ones they may name. A wrong
    literal here is invisible until an agent 401s at invoke time on real AWS.
    """

    # base_stack.py:201-400 (exec_role.add_to_policy calls, in order)
    EXECUTION_SIDS = (
        "AgentCoreDataPlane",
        "EcrPull",
        "EcrAuth",
        "SkillBundleObjects",
        "SkillBundleList",
        "GatewayAndRuntimeRead",
        "IdentityVaultSecrets",
        "Telemetry",
    )
    # base_stack.py:459-555, minus the two demo-target statements a boto3
    # bootstrap cannot deploy (hr_lambda.grant_invoke, InvokeRestTargets).
    GATEWAY_SIDS = (
        "IdentityVault",
        "InvokeRuntimeTargets",
        "IdentitySecrets",
    )

    def _documents(self) -> dict[str, dict]:
        bucket = f"launchpad-artifacts-{ACCOUNT}-{REGION}"
        repo_arn = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/launchpad-agents"
        return {
            "execution": workspace_iam.execution_role_policy(
                ACCOUNT, REGION, bucket, repo_arn
            ),
            "gateway": workspace_iam.gateway_role_policy(ACCOUNT, REGION),
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

    def test_a_role_that_cannot_be_assumed_names_the_trust_policy(self, aws, monkeypatch):
        """`GetCallerIdentity` is the run's first signed call, so a cross-account
        workspace whose trust policy or ExternalId is wrong fails here — with the
        fix, not with a raw AssumeRole traceback."""
        _register()
        monkeypatch.setattr(
            aws.sts,
            "get_caller_identity",
            lambda: (_ for _ in ()).throw(_client_error("AccessDenied", "AssumeRole")),
        )
        with pytest.raises(wb.BootstrapError) as exc:
            wb.STAGES["validate-access"](_ctx())
        message = str(exc.value)
        assert "cannot assume this workspace's role" in message
        assert "trust policy" in message and "ExternalId" in message

    def test_a_credential_denial_during_a_probe_is_not_blamed_on_the_service(
        self, aws, monkeypatch
    ):
        """Credentials refresh lazily, so an AssumeRole denial can land inside any
        probe; reporting it as `ListGateways failed` would send the operator after
        an AgentCore permission that is fine."""
        _register()
        monkeypatch.setattr(
            aws.control,
            "list_gateways",
            lambda **_kw: (_ for _ in ()).throw(_client_error("AccessDenied", "AssumeRole")),
        )
        with pytest.raises(wb.BootstrapError) as exc:
            wb.STAGES["validate-access"](_ctx())
        assert "cannot assume this workspace's role" in str(exc.value)

    def test_the_probes_name_the_resources_the_later_stages_own(self, aws, monkeypatch):
        """N17-obs130: an unfiltered list (`describe_repositories(maxResults=1)`,
        `list_projects()`, `list_roles(MaxItems=1)`) is authorised against the wildcard
        resource, which the hub's resource-scoped ops policy never grants — so the probes ask
        BY NAME for exactly the repository / project / role the storage, codebuild and iam
        stages create, and a not-found answer is the proof the permission is in place."""
        _register()
        calls: list[tuple[str, dict]] = []

        def recording(service, method):
            inner = getattr(getattr(aws, service), method)

            def call(**kw):
                calls.append((f"{service}.{method}", kw))
                return inner(**kw)

            return call

        for service, method in (("ecr", "describe_repositories"),
                                ("codebuild", "batch_get_projects"), ("iam", "get_role")):
            monkeypatch.setattr(getattr(aws, service), method, recording(service, method))
        logged: list[str] = []
        ctx = _ctx()
        ctx.log = logged.append
        detail = wb.STAGES["validate-access"](ctx)  # nothing exists yet: three not-founds
        assert detail == f"{REGION} usable · 5 services probed"
        assert calls == [
            ("ecr.describe_repositories", {"repositoryNames": [wb.ecr_repo_name()]}),
            ("codebuild.batch_get_projects", {"names": [wb.codebuild_project_name()]}),
            ("iam.get_role", {"RoleName": workspace_iam.regional_role_name(
                workspace_iam.EXECUTION_ROLE_BASE, REGION)}),
            # the foreign-deployment check asks for the same project again (by name too)
            ("codebuild.batch_get_projects", {"names": [wb.codebuild_project_name()]}),
        ]
        assert [m for m in logged if "not created yet" in m] == [
            "ecr DescribeRepositories: not created yet — the probe's permission is in place",
            "iam GetRole: not created yet — the probe's permission is in place",
        ]
        # and the same probes pass once the resources exist (a resumed bootstrap; an existing
        # CodeBuild project the row does not own is the foreign-deployment refusal, tested below)
        aws.ecr.create_repository(repositoryName=wb.ecr_repo_name())
        aws.iam.preexisting(workspace_iam.regional_role_name(
            workspace_iam.EXECUTION_ROLE_BASE, REGION))
        logged.clear()
        assert wb.STAGES["validate-access"](ctx) == f"{REGION} usable · 5 services probed"
        assert not [m for m in logged if "not created yet" in m]

    def test_the_probes_never_use_the_unfiltered_list_shapes(self):
        """The three wildcard-resource calls N17-obs130 named must stay out of the probe
        table: `_PROBES` is what the permission manifest is checked against."""
        operations = {(service, operation) for service, operation, _call in wb._PROBES}
        assert operations == {
            ("bedrock-agentcore-control", "ListGateways"), ("s3", "ListBuckets"),
            ("ecr", "DescribeRepositories"), ("codebuild", "BatchGetProjects"), ("iam", "GetRole"),
        }
        assert not operations & {("ecr", "ListRepositories"), ("codebuild", "ListProjects"),
                                 ("iam", "ListRoles")}

    def test_a_denied_by_name_probe_still_fails_with_the_denial_sentence(self, aws, monkeypatch):
        _register()
        monkeypatch.setattr(
            aws.iam,
            "get_role",
            lambda **_kw: (_ for _ in ()).throw(_client_error("AccessDenied", "GetRole")),
        )
        with pytest.raises(wb.BootstrapError) as exc:
            wb.STAGES["validate-access"](_ctx())
        message = str(exc.value)
        assert message.startswith(f"iam GetRole failed in {REGION}: AccessDenied")
        assert "the hub's credentials must be able to provision this workspace" in message

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

    # -- N17-obs132: the three verdict classes of a probe failure -----------------------

    @staticmethod
    def _probe_ctx() -> tuple[wb.BootstrapContext, list[float], list[str]]:
        slept: list[float] = []
        logged: list[str] = []
        ctx = _ctx()
        ctx.timeouts.sleeper = slept.append
        ctx.log = logged.append
        return ctx, slept, logged

    @staticmethod
    def _raising(exc_factory, calls: list[int]):
        def call(**_kw):
            calls.append(1)
            raise exc_factory()

        return call

    @pytest.mark.parametrize(
        "exc_factory",
        [
            lambda: UnknownServiceError(service_name="ecr", known_service_names=["s3"]),
            lambda: NoRegionError(),
            lambda: _client_error("UnrecognizedClientException", "DescribeRepositories"),
        ],
        ids=["UnknownServiceError", "NoRegionError", "UnrecognizedClientException"],
    )
    def test_a_missing_endpoint_is_the_region_verdict_without_a_retry(self, aws, monkeypatch,
                                                                    exc_factory):
        """Class 1 — endpoint RESOLUTION failed (no such service / no region in botocore's
        tables) or the service's own 'not here' code: the region sentence, at once (a retry
        cannot make a region appear). N17-obs200 moved `EndpointConnectionError` out of this
        class — it is raised after resolution succeeded."""
        _register()
        calls: list[int] = []
        monkeypatch.setattr(aws.ecr, "describe_repositories", self._raising(exc_factory, calls))
        ctx, slept, _logged = self._probe_ctx()
        with pytest.raises(wb.BootstrapError) as exc:
            wb.STAGES["validate-access"](ctx)
        message = str(exc.value)
        assert message.startswith(f"ecr is not usable in {REGION} (DescribeRepositories: ")
        assert "region not supported" in message
        assert "transient" not in message
        assert calls == [1] and slept == []

    @pytest.mark.parametrize(
        ("exc_factory", "named"),
        [
            (lambda: ReadTimeoutError(endpoint_url="https://ecr.secret-host.example/x"),
             "ReadTimeoutError"),
            (lambda: ConnectTimeoutError(endpoint_url="https://ecr.secret-host.example/x"),
             "ConnectTimeoutError"),
            (lambda: _client_error("ThrottlingException", "DescribeRepositories"),
             "ClientError: ThrottlingException (DescribeRepositories)"),
            (lambda: _client_error("SlowDown", "DescribeRepositories"),
             "ClientError: SlowDown (DescribeRepositories)"),
            (lambda: _client_error("InternalError", "DescribeRepositories"),
             "ClientError: InternalError (DescribeRepositories)"),
            # an unknown code with a 5xx status is a server-side failure too
            (lambda: ClientError({"Error": {"Code": "SomethingNew", "Message": "x"},
                                  "ResponseMetadata": {"HTTPStatusCode": 503}},
                                 "DescribeRepositories"),
             "ClientError: SomethingNew (DescribeRepositories)"),
        ],
        ids=["ReadTimeoutError", "ConnectTimeoutError", "ThrottlingException", "SlowDown",
             "InternalError", "unknown-code-503"],
    )
    def test_a_transient_failure_is_retried_once_then_named_as_transient(self, aws, monkeypatch,
                                                                        exc_factory, named):
        """Class 2 — a transport timeout, a throttle or a 5xx: ONE bounded retry (the injectable
        sleeper, `Timeouts.probe_retry_delay_s`), then a BootstrapError that names the service,
        the operation and the redacted cause and says "transient — retry" — never the region
        sentence, never botocore's message (the endpoint URL)."""
        _register()
        calls: list[int] = []
        monkeypatch.setattr(aws.ecr, "describe_repositories", self._raising(exc_factory, calls))
        ctx, slept, logged = self._probe_ctx()
        with pytest.raises(wb.BootstrapError) as exc:
            wb.STAGES["validate-access"](ctx)
        message = str(exc.value)
        assert message.startswith(f"ecr DescribeRepositories failed in {REGION} after 2 attempts")
        assert f"({named})" in message and "transient — retry" in message
        assert "region not supported" not in message
        assert "the hub's credentials must be able" not in message
        assert "secret-host" not in message  # redacted_error: type / code only, never the text
        assert calls == [1, 1]
        assert slept == [ctx.timeouts.probe_retry_delay_s]
        assert [m for m in logged if "retrying once" in m] == [
            f"ecr DescribeRepositories: transient failure ({named}) — retrying once in "
            f"{ctx.timeouts.probe_retry_delay_s:g}s"
        ]

    # -- N17-obs200: the fourth verdict — the endpoint resolved but cannot be reached -------

    def test_an_unreachable_endpoint_is_retried_once_then_named_as_network_not_region(
            self, aws, monkeypatch):
        """Class 4 — `EndpointConnectionError` (resolution succeeded, connect failed: a DNS /
        network outage on the hub's side, even to a supported region): ONE bounded retry like a
        transient, then a DISTINCT sentence that says unreachable from this host — network/DNS,
        not the region — with the host redacted to the service and the region; never the region
        sentence, never the plain transient one, never the denial."""
        _register()
        calls: list[int] = []
        monkeypatch.setattr(
            aws.ecr, "describe_repositories",
            self._raising(lambda: EndpointConnectionError(
                endpoint_url="https://api.ecr.secret-host.example/x"), calls),
        )
        ctx, slept, logged = self._probe_ctx()
        with pytest.raises(wb.BootstrapError) as exc:
            wb.STAGES["validate-access"](ctx)
        message = str(exc.value)
        assert message.startswith(
            f"ecr endpoint (ecr · {REGION}) unreachable from this host after 2 attempts "
            "(DescribeRepositories: EndpointConnectionError) — network/DNS, not the region")
        assert "region not supported" not in message
        assert "transient — retry" not in message
        assert "the hub's credentials must be able" not in message
        assert "secret-host" not in message and "https://" not in message  # redacted host
        assert calls == [1, 1]
        assert slept == [ctx.timeouts.probe_retry_delay_s]
        assert [m for m in logged if "retrying once" in m] == [
            "ecr DescribeRepositories: endpoint unreachable (EndpointConnectionError) — "
            f"retrying once in {ctx.timeouts.probe_retry_delay_s:g}s"
        ]

    def test_an_unreachable_endpoint_that_clears_on_the_retry_passes_the_stage(self, aws,
                                                                               monkeypatch):
        _register()
        inner = aws.ecr.describe_repositories
        calls: list[int] = []

        def flaky(**kw):
            calls.append(1)
            if len(calls) == 1:
                raise EndpointConnectionError(endpoint_url="https://ecr.example/x")
            return inner(**kw)

        monkeypatch.setattr(aws.ecr, "describe_repositories", flaky)
        ctx, slept, _logged = self._probe_ctx()
        assert wb.STAGES["validate-access"](ctx) == f"{REGION} usable · 5 services probed"
        assert calls == [1, 1] and len(slept) == 1

    def test_a_transient_failure_that_clears_on_the_retry_passes_the_stage(self, aws,
                                                                           monkeypatch):
        _register()
        inner = aws.ecr.describe_repositories
        calls: list[int] = []

        def flaky(**kw):
            calls.append(1)
            if len(calls) == 1:
                raise ReadTimeoutError(endpoint_url="https://ecr.example/x")
            return inner(**kw)

        monkeypatch.setattr(aws.ecr, "describe_repositories", flaky)
        ctx, slept, _logged = self._probe_ctx()
        assert wb.STAGES["validate-access"](ctx) == f"{REGION} usable · 5 services probed"
        assert calls == [1, 1] and len(slept) == 1

    def test_a_denial_is_never_retried(self, aws, monkeypatch):
        """Class 3 — the service answered: a refusal is final (the AccessDenied propagation
        retry is the stage-level `retry_access_denied`, not the probe's)."""
        _register()
        calls: list[int] = []
        monkeypatch.setattr(
            aws.ecr, "describe_repositories",
            self._raising(lambda: _client_error("AccessDeniedException", "DescribeRepositories"),
                          calls),
        )
        ctx, slept, _logged = self._probe_ctx()
        with pytest.raises(wb.BootstrapError) as exc:
            wb.STAGES["validate-access"](ctx)
        assert str(exc.value).startswith(
            f"ecr DescribeRepositories failed in {REGION}: AccessDeniedException")
        assert calls == [1] and slept == []

    def test_the_transient_region_and_unreachable_code_sets_are_disjoint(self):
        sets = (wb._TRANSIENT_CODES, wb._REGION_UNSUPPORTED_CODES, wb._ENDPOINT_UNREACHABLE_CODES)
        for i, a in enumerate(sets):
            for b in sets[i + 1:]:
                assert not a & b
        assert {"ReadTimeoutError", "ConnectTimeoutError"} <= wb._TRANSIENT_CODES
        # N17-obs200: the region set holds the endpoint-RESOLUTION failures only
        assert {"UnknownServiceError", "NoRegionError"} <= wb._REGION_UNSUPPORTED_CODES
        assert wb._ENDPOINT_UNREACHABLE_CODES == {"EndpointConnectionError"}

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
            ("project", "CodeBuild project launchpad-agent-builder"),
        ],
    )
    def test_every_marker_resource_triggers_the_refusal(self, aws, seed, expected):
        _register()
        aws.codebuild.projects["launchpad-agent-builder"] = {"name": "x"}

        with pytest.raises(wb.ForeignResourceError) as exc:
            wb.STAGES["validate-access"](_ctx())
        assert expected in str(exc.value)

    def test_resources_this_workspace_already_recorded_are_its_own(self, aws):
        """Which is what makes validate-access re-runnable after a partial run."""
        gateway_id = aws.control.preexisting_gateway("launchpad-gw")
        aws.codebuild.projects["launchpad-agent-builder"] = {"name": "x"}
        _register(
            resources={
                "gateway_id": gateway_id,
                "codebuild_project": "launchpad-agent-builder",
            }
        )
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

    # N11-r3-bug5 (live, default env 2026-10-10): the second `make bootstrap` found the
    # Skill Lab worker role from the first run, fell back to ListRoleTags, and the ops
    # role was denied → the stage failed after the AccessDenied retries.
    def test_a_tagged_role_is_adopted_from_get_role_without_listing_tags(self, aws, monkeypatch):
        _register()
        name = f"launchpad-agent-execution-role-{REGION}"
        aws.iam.preexisting(name, {"launchpad:workspace": WS_ID})
        monkeypatch.setattr(
            aws.iam, "list_role_tags",
            lambda **_kw: (_ for _ in ()).throw(_client_error("AccessDenied", "ListRoleTags")),
        )
        wb.STAGES["iam"](_ctx())  # GetRole already carried the tag; no listing needed
        assert name not in aws.iam.created

    def test_a_denied_tag_listing_adopts_with_a_warning_instead_of_failing(self, aws, monkeypatch):
        _register()
        name = f"launchpad-agent-execution-role-{REGION}"
        aws.iam.preexisting(name)  # GetRole answers without Tags → fallback → denied
        monkeypatch.setattr(
            aws.iam, "list_role_tags",
            lambda **_kw: (_ for _ in ()).throw(_client_error("AccessDenied", "ListRoleTags")),
        )
        logs: list[str] = []
        from app.services import workspace_iam

        arn = workspace_iam.ensure_role(
            aws.iam, workspace_id=WS_ID, role_name=name, trust_policy={"Version": "2012-10-17"},
            inline_policies={}, description="d", log=logs.append,
        )
        assert arn.endswith(name)
        assert any(line.startswith("WARNING: adopting role") for line in logs)
        assert name not in aws.iam.retagged  # unverified → not re-stamped either

    def test_an_identical_trust_policy_is_not_rewritten_on_adopt(self, aws):
        from app.services import workspace_iam

        trust = {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "sts:AssumeRole", "Principal": {"Service": "x"}}
            ],
        }
        kwargs = dict(
            workspace_id=WS_ID, role_name="launchpad-skill-lab-worker-r",
            trust_policy=trust, inline_policies={}, description="d",
        )
        workspace_iam.ensure_role(aws.iam, **kwargs)
        workspace_iam.ensure_role(aws.iam, **kwargs)  # second bootstrap: adopt
        assert getattr(aws.iam, "trust_updates", []) == []
        workspace_iam.ensure_role(aws.iam, **{**kwargs, "trust_policy": {**trust, "Id": "v2"}})
        assert aws.iam.trust_updates == ["launchpad-skill-lab-worker-r"]


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

    def test_a_stage_denied_right_after_an_iam_change_is_retried(self, aws, monkeypatch):
        """N11-obs11: IAM is eventually consistent — a stage denied once and allowed on
        the next call must not fail the job. The runner re-runs the (idempotent) stage
        after the back-off; the retry is visible in the job log."""
        from botocore.exceptions import ClientError

        _register()
        calls: list[int] = []
        slept: list[float] = []
        real_gateway = wb.STAGES["gateway"]

        def flaky(ctx):
            calls.append(1)
            if len(calls) == 1:
                raise ClientError(
                    {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
                    "CreateGateway",
                )
            return real_gateway(ctx)

        monkeypatch.setitem(wb.STAGES, "gateway", flaky)
        timeouts = _fast()
        timeouts.sleeper = slept.append
        timeouts.access_denied_delay_s = 12
        job_id = _queue_job()

        wb.execute_bootstrap_job(job_id, timeouts=timeouts)

        job = _job_row(job_id)
        assert job.status == "succeeded", job.error
        assert _stage_status(job)["gateway"] == "succeeded"
        assert len(calls) == 2 and slept == [12]
        assert "retry 1/2 in 12s" in job.log
        assert len(aws.control.gateways) == 1  # the re-run adopted, not duplicated

    def test_a_stage_still_denied_after_the_last_attempt_fails_the_job(self, aws, monkeypatch):
        from botocore.exceptions import ClientError

        _register()
        calls: list[int] = []

        def denied(_ctx):
            calls.append(1)
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "m"}}, "CreateGateway")

        monkeypatch.setitem(wb.STAGES, "gateway", denied)
        timeouts = _fast()
        timeouts.access_denied_attempts = 2
        job_id = _queue_job()

        wb.execute_bootstrap_job(job_id, timeouts=timeouts)

        job = _job_row(job_id)
        assert job.status == "failed" and len(calls) == 2
        assert _stage_status(job)["gateway"] == "failed"
        assert "AccessDenied" in job.error

    def test_a_failed_stage_fails_the_row_and_keeps_partial_resources(
        self, aws, monkeypatch
    ):
        _register()
        monkeypatch.setattr(
            aws.control,
            "create_gateway",
            lambda **_kw: (_ for _ in ()).throw(RuntimeError("gateway refused")),
        )
        job_id = _queue_job()

        wb.execute_bootstrap_job(job_id, timeouts=_fast())

        job = _job_row(job_id)
        assert job.status == "failed"
        assert "gateway refused" in (job.error or "")
        assert _stage_status(job) == {
            "validate-access": "succeeded",
            "iam": "succeeded",
            "model-credential": "succeeded",
            "storage": "succeeded",
            "codebuild": "succeeded",
            "gateway": "failed",
            "registry": "pending",
            "memory": "pending",
            "skill-lab": "pending",
            "observability": "pending",
            "policy": "pending",
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
                aws.control,
                "create_gateway",
                lambda **_kw: (_ for _ in ()).throw(RuntimeError("gateway refused")),
            )
            wb.execute_bootstrap_job(_queue_job(), timeouts=_fast())
        assert _row().bootstrap_status == "failed"
        roles_after_first_run = list(aws.iam.created)

        second = _queue_job()
        assert _stage_status(_job_row(second)) == {
            "validate-access": "succeeded",
            "iam": "succeeded",
            "model-credential": "succeeded",
            "storage": "succeeded",
            "codebuild": "succeeded",
            "gateway": "pending",
            "registry": "pending",
            "memory": "pending",
            "skill-lab": "pending",
            "observability": "pending",
            "policy": "pending",
            "finalize": "pending",
        }

        wb.execute_bootstrap_job(second, timeouts=_fast())

        assert _job_row(second).status == "succeeded"
        assert _row().bootstrap_status == "ready"
        # the skipped stages did not provision a second set of anything
        assert aws.iam.created == roles_after_first_run
        assert len(aws.s3.created) == 1
        assert aws.control.created_gateways == ["launchpad-gw"]

    def test_a_resumed_job_reads_the_previous_runs_identifiers(self, aws, monkeypatch):
        """The context is rebuilt from the row, so a stage after the interruption
        finds what earlier stages recorded rather than re-deriving it."""
        _register()
        with monkeypatch.context() as patched:
            patched.setitem(
                wb.STAGES,
                "codebuild",
                lambda _ctx: (_ for _ in ()).throw(RuntimeError("stop")),
            )
            wb.execute_bootstrap_job(_queue_job(), timeouts=_fast())

        second = _queue_job()
        wb.execute_bootstrap_job(second, timeouts=_fast())

        # the gateway stage needs iam's output; it only had the row
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
            lambda _db, row, **_kw: (_ for _ in ()).throw(wb.BootstrapConflict(row.id)),
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


# ── D21 (M1-b): the model-credential stage's registry pass (SSM-backed providers) ─────────


@pytest.mark.siliconflow(registry=True)
def test_model_credential_stage_registers_a_registry_provider_from_its_ssm_parameter(aws):
    """The built-in SiliconFlow row is `credential_source: ssm`: the stage reads
    `launchpad-siliconflow-api-key` from the WORKSPACE's SSM, creates the Identity provider
    there and records `source: ssm` + the parameter version — the value never lands on the
    row. Without the parameter the env pass's "not configured" answer stands (the
    `TestModelCredentialStage` strings above)."""
    _register()
    aws.ssm.parameters["launchpad-siliconflow-api-key"] = ("sf-KEY-0123456789", 4)
    detail = wb.STAGES["model-credential"](_ctx())
    assert detail == ("launchpad-global-bedrock · created; "
                      "launchpad-siliconflow · created · SSM v4")
    assert aws.ssm.calls == ["launchpad-siliconflow-api-key"]
    assert aws.control.provider_calls[-2:] == ["get:launchpad-siliconflow",
                                               "create:launchpad-siliconflow"]
    resources = context_for_workspace(WS_ID).resources
    record = resources["model_credentials"]["siliconflow"]
    assert record["configured"] is True and record["key_version"] == 4
    assert record["source"] == "ssm" and record["name"] == "launchpad-siliconflow"
    assert record["arn"].endswith("apikeycredentialprovider/launchpad-siliconflow")
    assert resources["model_credentials"]["bedrock"]["configured"] is True
    assert "sf-KEY-0123456789" not in json.dumps(resources)

    # second pass, parameter unchanged (D21-obs5): the spoke's own map holds v4 → Identity
    # is only read, the stage says "up to date"; the env-sourced Bedrock key has no version
    # to compare and keeps rotating in place (that path never journals)
    detail = wb.STAGES["model-credential"](_ctx())
    assert detail == ("launchpad-global-bedrock · reused (key rotated); "
                      "launchpad-siliconflow · reused (up to date) · SSM v4")
    assert aws.control.provider_calls[-1] == "get:launchpad-siliconflow"
    assert "update:launchpad-siliconflow" not in aws.control.provider_calls
    # third pass: rotation in place, idempotent resource map apart from the version
    aws.ssm.parameters["launchpad-siliconflow-api-key"] = ("sf-KEY-rotated", 5)
    detail = wb.STAGES["model-credential"](_ctx())
    assert detail == ("launchpad-global-bedrock · reused (key rotated); "
                      "launchpad-siliconflow · reused (key rotated) · SSM v5")
    assert aws.control.provider_calls[-1] == "update:launchpad-siliconflow"
    assert context_for_workspace(WS_ID).resources["model_credentials"]["siliconflow"][
        "key_version"] == 5
    # D21-obs4: a SPOKE pass never writes the hub-global row's key_version / journal — the
    # version lives on the spoke's own resource map (above); the hub pass owns the row.
    from sqlalchemy import select

    from app.models.model_registry import ModelProvider
    from app.services import model_registry

    db = SessionLocal()
    try:
        row = db.scalar(select(ModelProvider).where(ModelProvider.name == "siliconflow"))
        assert row.key_version is None and row.key_modified_at is None
        assert not [c for c in model_registry.list_changes(db, limit=50)
                    if c.operation == "provider.credential"]
    finally:
        db.close()
