"""Workspace bootstrap as a resumable job.

`make bootstrap` provisions the hub with CDK plus a linear boto3 pass. A
workspace registered from the console lives in a region where no stack is
deployed and nobody can run `cdk deploy` from a web backend, so the same
substrate is provisioned here with boto3 only, as a staged job:

    validate-access → iam → storage → codebuild → cognito
    → gateway → memory → registry → skill-lab → observability → finalize

The shape follows `deployer/pipeline.py` on purpose — ordered stages, per-stage
status persisted (here on `Job.payload["stages"]`, the same records the deploy
pipeline keeps on `Deployment.stages`), JSONL events on `Job.log`, and a resume
that skips whatever already succeeded. Every stage is idempotent (list-or-get
then create, the `ensure_*` contract from `bootstrap.py`) and **records the
identifiers it produced on the workspace row as it goes**, so a resumed run sees
the previous run's outputs instead of re-deriving them.

What this deliberately does not provision, and why:

* **policy engine** — operator-managed through Governance since the bootstrap
  flow made it opt-in (`d071afa`); governance reads the live gateway attachment.
* **KB gateway** — created lazily on first managed-KB use (`kb_gateway.py`).
* **the demo Build-Tools layer** (hr-database Lambda, office-facts REST API) —
  CDK-shipped sample *code*, so a workspace's gateway simply has no demo targets.
* **demo Cognito users** — a workspace is an operator environment, not a lab.

The job is credential-agnostic: it runs with the hub's own credentials for a
workspace in this account and through an assumed spoke role for one in another,
and the only stage that knows the difference is `validate-access`, which reports
a role that cannot be assumed. Everything else goes through
`ctx.workspace.client(...)`, and the context is the one thing that knows where
those clients point.
"""

import json
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.regions import partition_for_region
from app.models.ledger import Job, Workspace
from app.services import (
    aws_clients,
    cost_tags,
    gateway_bootstrap,
    policy_bootstrap,
    project_telemetry,
    workspace_iam,
)
from app.services import bootstrap as hub_bootstrap
from app.services.agent_iam import retry_iam_propagation
from app.services.policy_identity import POLICY_GROUPS
from app.services.workspace import (
    WorkspaceContext,
    context_for_workspace,
    merge_workspace_resources,
)

JOB_TYPE = "bootstrap_workspace"

STAGE_ORDER = [
    "validate-access",
    "iam",
    "storage",
    "codebuild",
    "cognito",
    "gateway",
    "memory",
    "registry",
    "skill-lab",
    "observability",
    "finalize",
]

STATUS_REGISTERED = "registered"
STATUS_BOOTSTRAPPING = "bootstrapping"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

# Region-scoped names, identical to the hub's — one workspace per (account,
# region) is a UNIQUE constraint on the table, so no discriminator is needed.
ECR_REPO_NAME = "launchpad-agents"
CODEBUILD_PROJECT_NAME = "launchpad-agent-builder"
USER_POOL_NAME = "launchpad-users"
CONSOLE_CLIENT_NAME = "launchpad-console"
M2M_CLIENT_NAME = "launchpad-agent-m2m"
RESOURCE_SERVER_ID = "launchpad-gw"
GATEWAY_SCOPE_NAME = "invoke"

# ARM64 build image + compute matching infra/stacks/base_stack.py:95-100
# (LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0 resolves to exactly this
# image id — note the missing "2": amazonlinux2-aarch64-standard:3.0 is the
# Amazon Linux 2 family, a different image).
CODEBUILD_IMAGE = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
CODEBUILD_COMPUTE = "BUILD_GENERAL1_SMALL"
CODEBUILD_TIMEOUT_MINUTES = 30

# Written by `finalize`; a workspace missing any of these cannot deploy or invoke.
# `registry_id` is absent on purpose: Registry access can be denied account-wide
# and the console degrades for that (REGISTRY_ACCESS_DENIED_REASON).
REQUIRED_RESOURCE_KEYS = (
    "execution_role_arn",
    "gateway_role_arn",
    "kb_role_arn",
    "artifacts_bucket",
    "ecr_repo",
    "ecr_repo_uri",
    "codebuild_project",
    "user_pool_id",
    "user_pool_client_id",
    "m2m_client_id",
    "gateway_id",
    "gateway_arn",
    "gateway_url",
    "oauth_provider_arn",
    "memory_id",
    "memory_arn",
)

_ACCESS_DENIED_CODES = frozenset(
    {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
)
# What AWS answers when a service has no endpoint (or no such operation) in the
# target region. Anything here means "this region cannot host a workspace", which
# is a different operator action than a permissions problem.
_REGION_UNSUPPORTED_CODES = frozenset(
    {
        "UnrecognizedClientException",
        "InvalidClientTokenId",
        "InvalidAction",
        "UnknownOperationException",
        "OptInRequired",
        "EndpointConnectionError",
    }
)


class BootstrapError(RuntimeError):
    """A stage failed for a reason the operator can act on."""


class BootstrapConflict(RuntimeError):
    """Another run already owns this workspace's bootstrap."""


ForeignResourceError = workspace_iam.ForeignResourceError


@dataclass
class Timeouts:
    """Waiter budgets, injectable so tests never wait on a stub."""

    memory_s: int = 300
    gateway_s: int = 300
    iam_attempts: int = 6
    iam_delay_s: int = 10
    sleeper: Callable[[float], None] = time.sleep


@dataclass
class BootstrapContext:
    """One bootstrap run. `workspace` is rebuilt from the row at job start, so a
    resumed run reads the identifiers the interrupted one already wrote."""

    workspace_id: str
    job_id: str
    workspace: WorkspaceContext
    timeouts: Timeouts = field(default_factory=Timeouts)
    log: Callable[[str], None] = lambda _msg: None

    @property
    def resources(self) -> dict[str, Any]:
        return self.workspace.resources

    @property
    def account_id(self) -> str:
        return self.workspace.account_id

    @property
    def region(self) -> str:
        return self.workspace.region

    def record(self, values: dict[str, Any]) -> None:
        """Persist identifiers on the workspace row the moment they exist.

        Per stage rather than per job: a job killed after `storage` must leave the
        bucket and repository it made recorded, or the next run would provision a
        second set instead of adopting them.
        """
        merge_workspace_resources(self.workspace, values)

    def require(self, key: str) -> str:
        value = str(self.resources.get(key) or "")
        if not value:
            raise BootstrapError(
                f"'{key}' is missing from this workspace's resource map — the stage "
                "that produces it has not succeeded yet"
            )
        return value


# ── job + stage bookkeeping ────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _new_stages() -> list[dict[str, Any]]:
    return [{"name": name, "status": "pending", "detail": ""} for name in STAGE_ORDER]


def _append_log(db: Session, job_id: str, stage: str, message: str, level: str = "info") -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    line = json.dumps(
        {"ts": _now_iso(), "stage": stage, "level": level, "msg": message},
        ensure_ascii=False,
    )
    job.log = (job.log + "\n" + line) if job.log else line
    db.commit()


def _set_stage(
    db: Session, job_id: str, stage: str, status: str, detail: str = ""
) -> None:
    job = db.get(Job, job_id)
    if job is None:
        return
    payload = dict(job.payload or {})
    stages = [dict(s) for s in payload.get("stages") or _new_stages()]
    for entry in stages:
        if entry["name"] != stage:
            continue
        entry["status"] = status
        if detail:
            entry["detail"] = detail
        if status == "running":
            entry["started_at"] = _now_iso()
        if status in ("succeeded", "skipped", "failed"):
            entry["ended_at"] = _now_iso()
    payload["stages"] = stages
    # Reassigned, not mutated: SQLAlchemy does not track in-place JSON edits.
    job.payload = payload
    db.commit()


def job_stages(job: Job) -> list[dict[str, Any]]:
    """The per-stage records a console progress view renders."""
    return list((job.payload or {}).get("stages") or [])


def _set_workspace_status(db: Session, workspace_id: str, status: str) -> None:
    row = db.get(Workspace, workspace_id)
    if row is None:
        return
    row.bootstrap_status = status
    db.commit()


def latest_job(db: Session, workspace_id: str) -> Job | None:
    return (
        db.query(Job)
        .filter(Job.type == JOB_TYPE, Job.workspace_id == workspace_id)
        .order_by(Job.created_at.desc(), Job.id.desc())
        .first()
    )


def create_bootstrap_job(db: Session, row: Workspace) -> Job:
    """Queue a bootstrap run for `row`, carrying forward what already succeeded.

    A retry after a failure is a resume, not a restart: the stages the previous
    job completed stay marked so the new job skips straight to the first one that
    did not, and so the console shows what is left rather than replaying every
    stage in green.

    Raises `BootstrapConflict` when the workspace is already bootstrapping or
    ready. The claim is a **conditional UPDATE**, not a read-then-write: two
    concurrent requests both pass the router's status check (sync handlers run on
    a threadpool), and two runs against one environment race on every create —
    `create_user_pool` would make a second pool of the same name, `ensure_memory`
    a second memory. Only one of the two UPDATEs can match.
    """
    claimed = db.execute(
        update(Workspace)
        .where(
            Workspace.id == row.id,
            Workspace.bootstrap_status.not_in((STATUS_BOOTSTRAPPING, STATUS_READY)),
        )
        .values(bootstrap_status=STATUS_BOOTSTRAPPING)
        .execution_options(synchronize_session="fetch")
    ).rowcount
    if not claimed:
        db.rollback()
        raise BootstrapConflict(row.id)
    previous = latest_job(db, row.id)
    done = {
        entry["name"]
        for entry in (job_stages(previous) if previous else [])
        if entry.get("status") in ("succeeded", "skipped")
    }
    stages = _new_stages()
    for entry in stages:
        if entry["name"] in done:
            entry["status"] = "succeeded"
            entry["detail"] = "already provisioned by an earlier run"
    job = Job(
        workspace_id=row.id,
        type=JOB_TYPE,
        payload={"workspace_id": row.id, "stages": stages},
    )
    db.add(job)
    db.commit()
    return job


# ── stage: validate-access ─────────────────────────────────────────────────

# One cheap call per service the later stages depend on. Ordered as they are
# needed, so the first failure names the earliest missing capability.
_PROBES: tuple[tuple[str, str, Callable[[Any], Any]], ...] = (
    ("bedrock-agentcore-control", "ListGateways", lambda c: c.list_gateways(maxResults=1)),
    ("agent-registry-control", "ListRegistries", lambda c: c.list_registries(maxResults=1)),
    ("cognito-idp", "ListUserPools", lambda c: c.list_user_pools(MaxResults=1)),
    ("s3", "ListBuckets", lambda c: c.list_buckets()),
    ("ecr", "DescribeRepositories", lambda c: c.describe_repositories(maxResults=1)),
    ("codebuild", "ListProjects", lambda c: c.list_projects()),
    ("iam", "ListRoles", lambda c: c.list_roles(MaxItems=1)),
)

# Registry can be denied account-wide; the console degrades for that instead of
# refusing to bootstrap (see bootstrap.REGISTRY_ACCESS_DENIED_REASON).
_DENIAL_TOLERATED = frozenset({"agent-registry-control"})


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", ""))
    return type(exc).__name__


def _probe(ctx: BootstrapContext, service: str, operation: str, call: Callable[[Any], Any]) -> str:
    try:
        call(ctx.workspace.client(service))
    except (ClientError, BotoCoreError) as exc:
        # BotoCoreError covers the failures that are not an API answer at all —
        # no endpoint for this service in this region, or no such service in the
        # installed botocore (UnknownServiceError). Those are region verdicts.
        if aws_clients.is_assume_role_failure(exc):
            # A credential refresh failed mid-probe: the denial is STS's, not this
            # service's, so reporting it as `<service> <operation> failed` would
            # send the operator after the wrong permission.
            raise BootstrapError(aws_clients.assume_role_diagnostic(exc)) from exc
        code = _error_code(exc)
        if code in _ACCESS_DENIED_CODES and service in _DENIAL_TOLERATED:
            ctx.log(f"{service} denied by account policy — the feature degrades")
            return "denied"
        if code in _REGION_UNSUPPORTED_CODES or isinstance(exc, BotoCoreError):
            raise BootstrapError(
                f"{service} is not usable in {ctx.region} ({operation}: {code}) — "
                "region not supported; register the workspace in a region where "
                "AgentCore and its dependencies are available"
            ) from exc
        raise BootstrapError(
            f"{service} {operation} failed in {ctx.region}: {code} — the hub's "
            "credentials must be able to provision this workspace"
        ) from exc
    return "ok"


def _find_gateway(control: Any, name: str) -> dict[str, Any] | None:
    token = None
    while True:
        kwargs = {"maxResults": 100} | ({"nextToken": token} if token else {})
        page = control.list_gateways(**kwargs)
        for gateway in page.get("items", []):
            if gateway.get("name") == name:
                return gateway
        token = page.get("nextToken")
        if not token:
            return None


def _find_memory(control: Any, name: str) -> dict[str, Any] | None:
    token = None
    while True:
        kwargs = {"maxResults": 100} | ({"nextToken": token} if token else {})
        page = control.list_memories(**kwargs)
        for memory in page.get("memories", []):
            memory_id = memory.get("id") or memory.get("memoryId") or ""
            if memory_id.startswith(f"{name}-"):
                return memory
        token = page.get("nextToken")
        if not token:
            return None


def _find_registry(registry_control: Any, name: str) -> dict[str, Any] | None:
    token = None
    while True:
        kwargs = {"maxResults": 100} | ({"nextToken": token} if token else {})
        page = registry_control.list_registries(**kwargs)
        for registry in page.get("registries", []):
            if registry.get("name") == name:
                return registry
        token = page.get("nextToken")
        if not token:
            return None


def _find_user_pool(cognito: Any, name: str) -> str | None:
    token = None
    while True:
        kwargs = {"MaxResults": 60} | ({"NextToken": token} if token else {})
        page = cognito.list_user_pools(**kwargs)
        for pool in page.get("UserPools", []):
            if pool.get("Name") == name:
                return str(pool["Id"])
        token = page.get("NextToken")
        if not token:
            return None


def _project_exists(codebuild: Any, name: str) -> bool:
    return bool(codebuild.batch_get_projects(names=[name]).get("projects"))


def _refuse_foreign_resources(ctx: BootstrapContext) -> None:
    """Refuse a region that already hosts a Launchpad deployment.

    A name match is *not* proof of ownership: this account runs an independent
    production install in another region, whose `launchpad-gw`, `launchpad_memory`
    and `launchpad-*-role-<region>` would all match. Adopting them would let two
    consoles drive one environment, so the only safe answer is to stop and let the
    operator pick another region.

    A resource this workspace already recorded is its own — that is what makes the
    check re-runnable after a partial bootstrap.
    """
    found: list[str] = []
    control = ctx.workspace.client("bedrock-agentcore-control")
    gateway_name = gateway_bootstrap.GATEWAY_NAME
    if not ctx.resources.get("gateway_id") and _find_gateway(control, gateway_name):
        found.append(f"gateway {gateway_name}")
    if not ctx.resources.get("memory_id") and _find_memory(control, hub_bootstrap.MEMORY_NAME):
        found.append(f"memory {hub_bootstrap.MEMORY_NAME}-*")
    if not ctx.resources.get("registry_id"):
        try:
            registry = _find_registry(
                ctx.workspace.client("agent-registry-control"), hub_bootstrap.REGISTRY_NAME
            )
        except ClientError as exc:
            # A lazy credential refresh denies with AccessDenied too, and tolerating
            # *that* would skip the registry marker instead of reporting a role that
            # cannot be assumed — the check would pass for the wrong reason.
            if aws_clients.is_assume_role_failure(exc):
                raise BootstrapError(aws_clients.assume_role_diagnostic(exc)) from exc
            if _error_code(exc) not in _ACCESS_DENIED_CODES:
                raise
            registry = None
        if registry:
            found.append(f"registry {hub_bootstrap.REGISTRY_NAME}")
    if not ctx.resources.get("user_pool_id") and _find_user_pool(
        ctx.workspace.client("cognito-idp"), USER_POOL_NAME
    ):
        found.append(f"Cognito user pool {USER_POOL_NAME}")
    if not ctx.resources.get("codebuild_project") and _project_exists(
        ctx.workspace.client("codebuild"), CODEBUILD_PROJECT_NAME
    ):
        found.append(f"CodeBuild project {CODEBUILD_PROJECT_NAME}")
    if found:
        raise ForeignResourceError(
            f"{ctx.region} already hosts a Launchpad deployment ({', '.join(found)}) "
            "that this workspace did not create — refusing to adopt it. Register "
            "the workspace in a region without one, or remove those resources."
        )


def _caller_identity(ctx: BootstrapContext) -> dict[str, Any]:
    """Who this workspace's credentials actually are.

    The first signed call of the run, so for a cross-account workspace this is
    where a broken trust policy or a wrong ExternalId detonates — the session is
    built without contacting STS. Caught here so the stage fails with the fix
    rather than with a raw AssumeRole traceback.
    """
    try:
        return dict(ctx.workspace.client("sts").get_caller_identity())
    except ClientError as exc:
        if aws_clients.is_assume_role_failure(exc):
            raise BootstrapError(aws_clients.assume_role_diagnostic(exc)) from exc
        raise


def _stage_validate_access(ctx: BootstrapContext) -> str:
    identity = _caller_identity(ctx)
    account = str(identity.get("Account") or "")
    if account != ctx.account_id:
        # Same-account workspace: this is the hub's own identity, so a mismatch
        # means the operator registered someone else's account number. Cross-
        # account: the assumed role answers with the spoke's account, so a
        # mismatch means role_arn points into a third account.
        raise BootstrapError(
            f"these credentials belong to account {account}, but the workspace "
            f"declares {ctx.account_id} — check the workspace's account id and, "
            "for a cross-account workspace, that role_arn names a role in it"
        )
    probed = []
    for service, operation, call in _PROBES:
        probed.append(f"{service}:{_probe(ctx, service, operation, call)}")
    ctx.log(f"probed {len(probed)} services in {ctx.region}")
    _refuse_foreign_resources(ctx)
    return f"{ctx.region} usable · {len(probed)} services probed"


# ── stage: iam ─────────────────────────────────────────────────────────────


def _ecr_repo_arn(ctx: BootstrapContext) -> str:
    repo = ctx.resources.get("ecr_repo") or ECR_REPO_NAME
    partition = partition_for_region(ctx.region)
    return f"arn:{partition}:ecr:{ctx.region}:{ctx.account_id}:repository/{repo}"


def _artifacts_bucket_name(ctx: BootstrapContext) -> str:
    """S3 names are globally unique, and this one already carries account+region —
    the CDK bucket uses exactly this shape."""
    return f"launchpad-artifacts-{ctx.account_id}-{ctx.region}"


def _stage_iam(ctx: BootstrapContext) -> str:
    """The three service roles. Runs before `storage` deliberately: an IAM policy
    may name a bucket or repository that does not exist yet, and the deploy path
    needs `execution_role_arn` for everything downstream."""
    iam = ctx.workspace.client("iam")
    bucket = _artifacts_bucket_name(ctx)
    repo_arn = _ecr_repo_arn(ctx)
    execution_role_arn = workspace_iam.ensure_role(
        iam,
        workspace_id=ctx.workspace_id,
        role_name=workspace_iam.regional_role_name(
            workspace_iam.EXECUTION_ROLE_BASE, ctx.region
        ),
        trust_policy=workspace_iam.service_trust_policy(
            "bedrock-agentcore.amazonaws.com", ctx.account_id
        ),
        inline_policies={
            "launchpad-agent-execution": workspace_iam.execution_role_policy(
                ctx.account_id, ctx.region, bucket, repo_arn
            )
        },
        description=(
            "Assumed by AgentCore Runtime/Harness workloads launched by Launchpad"
        ),
        log=ctx.log,
    )
    gateway_role_arn = workspace_iam.ensure_role(
        iam,
        workspace_id=ctx.workspace_id,
        role_name=workspace_iam.regional_role_name(
            workspace_iam.GATEWAY_ROLE_BASE, ctx.region
        ),
        trust_policy=workspace_iam.service_trust_policy(
            "bedrock-agentcore.amazonaws.com", ctx.account_id
        ),
        inline_policies={
            "launchpad-gateway": workspace_iam.gateway_role_policy(
                ctx.account_id, ctx.region
            )
        },
        description="Assumed by AgentCore Gateway to reach targets + identity vault",
        log=ctx.log,
    )
    kb_role_arn = workspace_iam.ensure_role(
        iam,
        workspace_id=ctx.workspace_id,
        role_name=workspace_iam.regional_role_name(workspace_iam.KB_ROLE_BASE, ctx.region),
        trust_policy=workspace_iam.service_trust_policy(
            "bedrock.amazonaws.com", ctx.account_id
        ),
        inline_policies={
            "launchpad-kb": workspace_iam.kb_role_policy(bucket)
        },
        description=(
            "Assumed by Bedrock Managed Knowledge Bases to ingest S3 data sources"
        ),
        log=ctx.log,
    )
    ctx.record(
        {
            "execution_role_arn": execution_role_arn,
            "gateway_role_arn": gateway_role_arn,
            "kb_role_arn": kb_role_arn,
        }
    )
    return "3 service roles ready"


# ── stage: storage ─────────────────────────────────────────────────────────


def _ensure_bucket(ctx: BootstrapContext, s3: Any, bucket: str) -> bool:
    """Create-or-adopt, then apply the CDK bucket's posture (idempotent puts)."""
    created = False
    params: dict[str, Any] = {"Bucket": bucket}
    if ctx.region != "us-east-1":
        # us-east-1 rejects a LocationConstraint naming itself.
        params["CreateBucketConfiguration"] = {"LocationConstraint": ctx.region}
    try:
        s3.create_bucket(**params)
        created = True
        ctx.log(f"created artifacts bucket {bucket}")
    except ClientError as exc:
        code = _error_code(exc)
        if code == "BucketAlreadyOwnedByYou":
            ctx.log(f"reusing artifacts bucket {bucket}")
        elif code == "BucketAlreadyExists":
            raise BootstrapError(
                f"S3 bucket {bucket} exists in another account — bucket names are "
                "globally unique; this one encodes the account and region, so the "
                "workspace's account_id is probably wrong"
            ) from exc
        else:
            raise
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
            ]
        },
    )
    # `enforce_ssl=True` in the CDK bucket is this policy.
    s3.put_bucket_policy(
        Bucket=bucket,
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "DenyInsecureTransport",
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "s3:*",
                        "Resource": [
                            f"arn:aws:s3:::{bucket}",
                            f"arn:aws:s3:::{bucket}/*",
                        ],
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    }
                ],
            }
        ),
    )
    return created


def _ensure_repository(ctx: BootstrapContext, ecr: Any, name: str) -> str:
    try:
        repo = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]
        ctx.log(f"reusing ECR repository {name}")
        if repo.get("repositoryArn"):
            cost_tags.tag_quietly(
                ctx.workspace, cost_tags.entry(cost_tags.ECR_REPOSITORY,
                                               arn=repo["repositoryArn"]),
                _shared_tags(ctx), ctx.log)
    except ClientError as exc:
        if _error_code(exc) != "RepositoryNotFoundException":
            raise
        repo = ecr.create_repository(
            repositoryName=name,
            # Scan on push so a deploy can be blocked on findings before the image
            # ever backs a runtime.
            imageScanningConfiguration={"scanOnPush": True},
            tags=cost_tags.tag_list(_shared_tags(ctx)),
        )["repository"]
        ctx.log(f"created ECR repository {name}")
    return str(repo["repositoryUri"])


def _shared_tags(ctx: BootstrapContext) -> dict[str, str]:
    """Cost tags of the workspace's own infrastructure (cost-tags design §1.2)."""
    return project_telemetry.shared_tags(ctx.workspace_id)


def _stage_storage(ctx: BootstrapContext) -> str:
    bucket = _artifacts_bucket_name(ctx)
    _ensure_bucket(ctx, ctx.workspace.client("s3"), bucket)
    repo_uri = _ensure_repository(ctx, ctx.workspace.client("ecr"), ECR_REPO_NAME)
    ctx.record(
        {
            "artifacts_bucket": bucket,
            "ecr_repo": ECR_REPO_NAME,
            "ecr_repo_uri": repo_uri,
        }
    )
    return f"{bucket} · {ECR_REPO_NAME}"


# ── stage: codebuild ───────────────────────────────────────────────────────

CODEBUILD_POLICY_NAME = "launchpad-agent-builder"


def codebuild_policy(
    account_id: str, region: str, bucket: str, ecr_repo_arn: str
) -> dict[str, Any]:
    """`workspace_iam.codebuild_role_policy`, with the push grant widened from the
    shared repository to it **and** the per-project repositories under it
    (`launchpad-agents/<pid>`, projects design §10.10): an isolated project's images
    are built by the same project, into its own repository."""
    policy = workspace_iam.codebuild_role_policy(
        account_id, region, bucket, ecr_repo_arn, CODEBUILD_PROJECT_NAME
    )
    for statement in policy["Statement"]:
        if statement.get("Sid") == "EcrPullPush":
            statement["Resource"] = [ecr_repo_arn, f"{ecr_repo_arn}/*"]
    return policy


def ensure_codebuild_project_repos(
    workspace: WorkspaceContext, log: Callable[[str], None] = lambda _m: None
) -> bool:
    """Put the widened `codebuild_policy` on the role the workspace's CodeBuild project
    runs as, for a workspace bootstrapped before the widening (or by the CDK stack,
    whose role carries `repo.grant_pull_push` on the shared repository only, and is not
    named `launchpad-*`, so the workspace role cannot write it). Called by the `ecr`
    stage of `project_provision` (`project_storage`); idempotent. Returns
    whether the role carries the grant now; a role that cannot be read or written is
    logged and reported False — the repository is still made, and the deploy's build
    stage then names the missing grant."""
    resources = workspace.resources or {}
    bucket = resources.get("artifacts_bucket")
    project = resources.get("codebuild_project") or CODEBUILD_PROJECT_NAME
    if not bucket:
        log("artifacts_bucket missing from the resource map — CodeBuild role not widened")
        return False
    repo = resources.get("ecr_repo") or ECR_REPO_NAME
    repo_arn = (
        f"arn:{partition_for_region(workspace.region)}:ecr:{workspace.region}:"
        f"{workspace.account_id}:repository/{repo}"
    )
    try:
        found = workspace.client("codebuild").batch_get_projects(names=[project])
        projects = found.get("projects") or []
        role_arn = str(projects[0].get("serviceRole") or "") if projects else ""
        if not role_arn:
            log(f"CodeBuild project {project} not found — CodeBuild role not widened")
            return False
        role_name = role_arn.rsplit("/", 1)[-1]
        # a plain put, like `workspace_iam.ensure_role`'s reconcile: PutRolePolicy is
        # idempotent and the workspace role is not granted GetRolePolicy
        workspace.client("iam").put_role_policy(
            RoleName=role_name, PolicyName=CODEBUILD_POLICY_NAME,
            PolicyDocument=json.dumps(codebuild_policy(
                workspace.account_id, workspace.region, bucket, repo_arn
            )),
        )
        log(f"CodeBuild role {role_name} may push to {repo}/*")
        return True
    except (ClientError, BotoCoreError) as exc:
        log(f"CodeBuild role not widened to {repo}/*: {_error_code(exc)} — re-run the "
            "workspace bootstrap, or grant its role push on the project repository")
        return False


def _stage_codebuild(ctx: BootstrapContext) -> str:
    bucket = ctx.require("artifacts_bucket")
    role_arn = workspace_iam.ensure_role(
        ctx.workspace.client("iam"),
        workspace_id=ctx.workspace_id,
        role_name=workspace_iam.regional_role_name(
            workspace_iam.CODEBUILD_ROLE_BASE, ctx.region
        ),
        trust_policy=workspace_iam.service_trust_policy(
            "codebuild.amazonaws.com", ctx.account_id, source_account=False
        ),
        inline_policies={
            CODEBUILD_POLICY_NAME: codebuild_policy(
                ctx.account_id, ctx.region, bucket, _ecr_repo_arn(ctx)
            )
        },
        description="Assumed by CodeBuild to build Launchpad agent images",
        log=ctx.log,
    )
    codebuild = ctx.workspace.client("codebuild")
    if _project_exists(codebuild, CODEBUILD_PROJECT_NAME):
        ctx.log(f"reusing CodeBuild project {CODEBUILD_PROJECT_NAME}")
        cost_tags.tag_quietly(
            ctx.workspace, cost_tags.entry(cost_tags.CODEBUILD_PROJECT,
                                           name=CODEBUILD_PROJECT_NAME),
            _shared_tags(ctx), ctx.log)
    else:
        # Every start_build overrides source and environment variables
        # (agentcore/codebuild.start_image_build), so the placeholder source here
        # only has to be a valid location — same as the CDK project's.
        retry_iam_propagation(
            lambda: codebuild.create_project(
                name=CODEBUILD_PROJECT_NAME,
                description="Launchpad ARM64 agent image builder",
                source={"type": "S3", "location": f"{bucket}/builds/placeholder/source.zip"},
                artifacts={"type": "NO_ARTIFACTS"},
                environment={
                    "type": "ARM_CONTAINER",
                    "image": CODEBUILD_IMAGE,
                    "computeType": CODEBUILD_COMPUTE,
                    "privilegedMode": True,  # docker build
                },
                serviceRole=role_arn,
                timeoutInMinutes=CODEBUILD_TIMEOUT_MINUTES,
                tags=cost_tags.tag_list(_shared_tags(ctx), "key", "value"),
            ),
            ctx.log,
            attempts=ctx.timeouts.iam_attempts,
            delay_s=ctx.timeouts.iam_delay_s,
            sleeper=ctx.timeouts.sleeper,
        )
        ctx.log(f"created CodeBuild project {CODEBUILD_PROJECT_NAME}")
    ctx.record({"codebuild_project": CODEBUILD_PROJECT_NAME})
    return CODEBUILD_PROJECT_NAME


# ── stage: cognito ─────────────────────────────────────────────────────────


def _ensure_user_pool(ctx: BootstrapContext, cognito: Any) -> str:
    existing = _find_user_pool(cognito, USER_POOL_NAME)
    if existing:
        ctx.log(f"reusing user pool {USER_POOL_NAME}")
        return existing
    created = cognito.create_user_pool(
        PoolName=USER_POOL_NAME,
        Policies={
            "PasswordPolicy": {
                "MinimumLength": 12,
                "RequireUppercase": True,
                "RequireLowercase": True,
                "RequireNumbers": True,
                "RequireSymbols": False,
            }
        },
        # sign_in_aliases(username=True, email=True) in the CDK pool.
        AliasAttributes=["email"],
        AdminCreateUserConfig={"AllowAdminCreateUserOnly": True},
    )["UserPool"]
    ctx.log(f"created user pool {USER_POOL_NAME}")
    return str(created["Id"])


def _ensure_resource_server(ctx: BootstrapContext, cognito: Any, pool_id: str) -> None:
    try:
        cognito.describe_resource_server(
            UserPoolId=pool_id, Identifier=RESOURCE_SERVER_ID
        )
        return
    except ClientError as exc:
        if _error_code(exc) != "ResourceNotFoundException":
            raise
    cognito.create_resource_server(
        UserPoolId=pool_id,
        Identifier=RESOURCE_SERVER_ID,
        Name=RESOURCE_SERVER_ID,
        Scopes=[
            {
                "ScopeName": GATEWAY_SCOPE_NAME,
                "ScopeDescription": "Invoke launchpad gateway tools",
            }
        ],
    )
    ctx.log(f"created resource server {RESOURCE_SERVER_ID}")


def _find_client(cognito: Any, pool_id: str, name: str) -> str | None:
    token = None
    while True:
        kwargs = {"UserPoolId": pool_id, "MaxResults": 60} | (
            {"NextToken": token} if token else {}
        )
        page = cognito.list_user_pool_clients(**kwargs)
        for client in page.get("UserPoolClients", []):
            if client.get("ClientName") == name:
                return str(client["ClientId"])
        token = page.get("NextToken")
        if not token:
            return None


def _ensure_console_client(ctx: BootstrapContext, cognito: Any, pool_id: str) -> str:
    existing = _find_client(cognito, pool_id, CONSOLE_CLIENT_NAME)
    if existing:
        return existing
    created = cognito.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName=CONSOLE_CLIENT_NAME,
        GenerateSecret=False,
        ExplicitAuthFlows=[
            "ALLOW_USER_PASSWORD_AUTH",
            "ALLOW_USER_SRP_AUTH",
            "ALLOW_REFRESH_TOKEN_AUTH",
        ],
        IdTokenValidity=8,
        AccessTokenValidity=8,
        TokenValidityUnits={"IdToken": "hours", "AccessToken": "hours"},
    )["UserPoolClient"]
    ctx.log(f"created user pool client {CONSOLE_CLIENT_NAME}")
    return str(created["ClientId"])


def _ensure_m2m_client(ctx: BootstrapContext, cognito: Any, pool_id: str) -> str:
    existing = _find_client(cognito, pool_id, M2M_CLIENT_NAME)
    if existing:
        return existing
    created = cognito.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName=M2M_CLIENT_NAME,
        GenerateSecret=True,  # the OAuth provider reads this secret
        AllowedOAuthFlows=["client_credentials"],
        AllowedOAuthFlowsUserPoolClient=True,
        AllowedOAuthScopes=[f"{RESOURCE_SERVER_ID}/{GATEWAY_SCOPE_NAME}"],
        SupportedIdentityProviders=["COGNITO"],
        AccessTokenValidity=1,
        TokenValidityUnits={"AccessToken": "hours"},
    )["UserPoolClient"]
    ctx.log(f"created user pool client {M2M_CLIENT_NAME}")
    return str(created["ClientId"])


def _ensure_domain(ctx: BootstrapContext, cognito: Any, pool_id: str) -> str:
    """Hosted-UI domain. The CDK prefix is `launchpad-{account}`; the region is
    folded in here so two workspaces in one account never contend for it."""
    prefix = f"launchpad-{ctx.account_id}-{ctx.region}"
    try:
        described = cognito.describe_user_pool_domain(Domain=prefix)
        if (described.get("DomainDescription") or {}).get("UserPoolId"):
            return prefix
    except ClientError as exc:
        if _error_code(exc) != "ResourceNotFoundException":
            raise
    try:
        cognito.create_user_pool_domain(Domain=prefix, UserPoolId=pool_id)
        ctx.log(f"created hosted-UI domain {prefix}")
    except ClientError as exc:
        if _error_code(exc) not in ("InvalidParameterException", "ResourceConflictException"):
            raise
        # A domain the describe above could not see but that create refuses is
        # somebody else's prefix. The hosted UI is only used by the policy demos,
        # so the workspace stays usable without it.
        ctx.log(f"hosted-UI domain {prefix} unavailable ({_error_code(exc)}) — skipped")
        return ""
    return prefix


def _ensure_groups(ctx: BootstrapContext, cognito: Any, pool_id: str) -> None:
    """`policy_identity` puts console identities into these groups so Cedar rules
    can evaluate `cognito:groups`; the add fails if the group does not exist."""
    for group in sorted(POLICY_GROUPS):
        try:
            cognito.create_group(
                UserPoolId=pool_id,
                GroupName=group,
                Description=f"Launchpad role: {group}",
            )
        except ClientError as exc:
            if _error_code(exc) != "GroupExistsException":
                raise


def _stage_cognito(ctx: BootstrapContext) -> str:
    cognito = ctx.workspace.client("cognito-idp")
    pool_id = _ensure_user_pool(ctx, cognito)
    ctx.record({"user_pool_id": pool_id})
    _ensure_resource_server(ctx, cognito, pool_id)
    console_client = _ensure_console_client(ctx, cognito, pool_id)
    ctx.record({"user_pool_client_id": console_client})
    m2m_client = _ensure_m2m_client(ctx, cognito, pool_id)
    ctx.record({"m2m_client_id": m2m_client})
    _ensure_groups(ctx, cognito, pool_id)
    domain = _ensure_domain(ctx, cognito, pool_id)
    if domain:
        ctx.record({"user_pool_domain": domain})
    return f"{pool_id} · console + m2m clients"


# ── stage: gateway ─────────────────────────────────────────────────────────


def _stage_gateway(ctx: BootstrapContext) -> str:
    """The shared MCP gateway plus outbound M2M auth — no demo targets.

    `hr-database` and `office-facts` are CDK-shipped sample code that only the hub
    has; a workspace's gateway starts empty and gains targets from the Registry.
    """
    control = ctx.workspace.client("bedrock-agentcore-control")
    gateway, created = gateway_bootstrap.ensure_gateway(
        control,
        role_arn=ctx.require("gateway_role_arn"),
        user_pool_id=ctx.require("user_pool_id"),
        client_id=ctx.require("user_pool_client_id"),
        region=ctx.region,
        timeout_s=ctx.timeouts.gateway_s,
    )
    ctx.record(
        {
            "gateway_id": gateway["id"],
            "gateway_arn": gateway["arn"],
            "gateway_url": gateway["url"],
        }
    )
    ctx.log(f"gateway {gateway['id']} {'created' if created else 'reused'}")
    # the shared gateway is workspace infrastructure (cost-tags design §1.2)
    cost_tags.tag_quietly(ctx.workspace, cost_tags.entry(cost_tags.GATEWAY,
                                                         arn=gateway["arn"]),
                          _shared_tags(ctx), ctx.log)
    m2m_client_id = ctx.require("m2m_client_id")
    gateway_bootstrap.ensure_gateway_allows_client(
        control, gateway["id"], m2m_client_id, timeout_s=ctx.timeouts.gateway_s
    )
    oauth_arn, oauth_created = gateway_bootstrap.ensure_oauth_provider(
        control,
        ctx.workspace.client("cognito-idp"),
        user_pool_id=ctx.require("user_pool_id"),
        m2m_client_id=m2m_client_id,
        region=ctx.region,
    )
    ctx.record({"oauth_provider_arn": oauth_arn})
    ctx.log(f"outbound M2M provider {'created' if oauth_created else 'reused'}")
    return f"{gateway['id']} · no demo targets"


# ── stage: memory ──────────────────────────────────────────────────────────


def _stage_memory(ctx: BootstrapContext) -> str:
    control = ctx.workspace.client("bedrock-agentcore-control")
    # CreateMemory validates memoryExecutionRoleArn server-side, and the role was
    # made minutes ago in this same job.
    memory, created = retry_iam_propagation(
        lambda: hub_bootstrap.ensure_memory(
            control,
            execution_role_arn=ctx.require("execution_role_arn"),
            timeout_s=ctx.timeouts.memory_s,
            tags=_shared_tags(ctx),
        ),
        ctx.log,
        attempts=ctx.timeouts.iam_attempts,
        delay_s=ctx.timeouts.iam_delay_s,
        sleeper=ctx.timeouts.sleeper,
    )
    ctx.record({"memory_id": memory["id"], "memory_arn": memory["arn"]})
    return f"{memory['id']} {'created' if created else 'reused'}"


# ── stage: registry ────────────────────────────────────────────────────────


def _stage_registry(ctx: BootstrapContext) -> str:
    registry, created = hub_bootstrap.ensure_registry(
        ctx.workspace.client("agent-registry-control")
    )
    if registry is None:
        # Same degradation as the hub: Registry features switch off, everything
        # else works. Empty values deliberately replace stale identifiers.
        ctx.record(
            {
                "registry_id": "",
                "registry_arn": "",
                "registry_unavailable_reason": (
                    hub_bootstrap.REGISTRY_ACCESS_DENIED_REASON
                ),
            }
        )
        return "unavailable · access denied by account policy"
    ctx.record(
        {
            "registry_id": registry["id"],
            "registry_arn": registry["arn"],
            "registry_unavailable_reason": "",
        }
    )
    return f"{registry['id']} {'created' if created else 'reused'}"


# ── stage: observability ───────────────────────────────────────────────────


def _ensure_spans_resource_policy(ctx: BootstrapContext) -> None:
    """Let X-Ray write into this region's `aws/spans` log group — the same
    helper the hub's `run_bootstrap` uses (see its docstring)."""
    placed = policy_bootstrap.ensure_spans_resource_policy(
        ctx.workspace.client("logs"), ctx.workspace.account_id, ctx.workspace.region
    )
    if placed:
        ctx.log("aws/spans resource policy placed for X-Ray")


def _stage_observability(ctx: BootstrapContext) -> str:
    """Transaction Search, which the hub's `run_bootstrap` also enables.

    It is a per-region X-Ray setting, so a workspace in another region starts with
    it off — and every trace/session/token view reads the `aws/spans` log group it
    populates, i.e. Observability would be silently empty without this.

    Degrades instead of failing, like the Registry stage: an environment whose
    agents deploy and invoke is worth having even where this account cannot
    change the region's trace destination. The reason lands in the stage detail
    and the job log, so it is visible rather than mysterious.
    """
    xray = ctx.workspace.client("xray")
    try:
        _ensure_spans_resource_policy(ctx)
        state = policy_bootstrap.ensure_transaction_search(xray)
    except (ClientError, BotoCoreError) as exc:
        code = _error_code(exc)
        ctx.log(f"Transaction Search unavailable ({code}) — traces will be empty")
        return f"unavailable · {code}"
    if not state.get("enabled"):
        return f"not active yet · {state.get('status') or 'unknown'}"
    return "transaction search active" + ("" if state.get("changed") else " (already)")


def repair_observability(workspace: WorkspaceContext, *, workspace_id: str,
                         log: Callable[[str], None] = lambda _msg: None) -> dict[str, Any]:
    """Re-run only the observability stage on an already-ready workspace.

    The stage degrades rather than fails (an environment that deploys and invokes is
    worth having), so a workspace can reach READY with Transaction Search off — e.g.
    when the role lacked a permission at bootstrap time. A full re-bootstrap is
    refused on a ready workspace, so without this the only fix was by hand. Returns
    the same detail string the stage records, plus whether it is active now.
    """
    ctx = BootstrapContext(workspace_id=workspace_id, job_id="repair-observability",
                           workspace=workspace, log=log)
    detail = _stage_observability(ctx)
    return {"detail": detail, "active": detail.startswith("transaction search active")}


# ── stage: finalize ────────────────────────────────────────────────────────


def _stage_finalize(ctx: BootstrapContext) -> str:
    missing = [key for key in REQUIRED_RESOURCE_KEYS if not ctx.resources.get(key)]
    if missing:
        raise BootstrapError(
            "the resource map is incomplete after every stage succeeded, which "
            f"means a stage recorded nothing: missing {', '.join(missing)}"
        )
    db = SessionLocal()
    try:
        _set_workspace_status(db, ctx.workspace_id, STATUS_READY)
    finally:
        db.close()
    return f"{len(REQUIRED_RESOURCE_KEYS)} required resources present · ready"


def _stage_skill_lab(ctx: BootstrapContext) -> str:
    """Skill Lab exec worker: role + content-addressed image + runtime.

    Keys stay out of REQUIRED_RESOURCE_KEYS by design — a workspace without the
    worker still deploys/invokes; Skill Lab just shows unprovisioned. The
    dedicated host venv is provisioned by the hub bootstrap (`make bootstrap`),
    not per workspace: it is host-local and shared.

    Degrades instead of failing, like the Registry and Observability stages, and
    for a stronger reason: a raised failure here leaves the workspace `failed`,
    and every non-read request against a workspace that is not `ready` is
    refused (routers/workspaces.py). One optional feature's CodeBuild or runtime
    problem must not cost the environment its deploy/invoke path."""
    from app.skill_lab import infra as skill_lab_infra

    try:
        resources = skill_lab_infra.ensure_skill_lab_worker(
            ctx.workspace, ctx.workspace_id, log=ctx.log
        )
    # RuntimeError covers ForeignResourceError and the wrappers' build/runtime
    # failures; OSError covers the waiters' TimeoutError.
    except (BotoCoreError, ClientError, OSError, RuntimeError) as exc:
        detail = _error_code(exc) or f"{type(exc).__name__}: {exc}"
        ctx.log(f"skill lab worker unavailable ({detail}) — Skill Lab stays unprovisioned")
        return f"unavailable · {detail}"
    ctx.record(resources)
    # Demo samples (vendored logtriage/report demos → registry records + sample
    # task sets). Same degrade posture: seeding problems must not fail the stage.
    try:
        from app.core.db import SessionLocal
        from app.skill_lab.samples import ensure_skill_lab_samples

        db = SessionLocal()
        try:
            samples_detail = ensure_skill_lab_samples(
                db, ctx.workspace_id, ctx.workspace, log=ctx.log
            )
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001 — optional demo content only
        ctx.log(f"sample seeding skipped ({type(exc).__name__}: {exc})")
        samples_detail = "samples skipped"
    return (
        f"{skill_lab_infra.WORKER_RUNTIME_NAME} · "
        f"{resources['skill_lab_worker_image_tag']} · {samples_detail}"
    )


STAGES: dict[str, Callable[[BootstrapContext], str]] = {
    "validate-access": _stage_validate_access,
    "iam": _stage_iam,
    "storage": _stage_storage,
    "codebuild": _stage_codebuild,
    "cognito": _stage_cognito,
    "gateway": _stage_gateway,
    "memory": _stage_memory,
    "registry": _stage_registry,
    "skill-lab": _stage_skill_lab,
    "observability": _stage_observability,
    "finalize": _stage_finalize,
}


# ── runner ─────────────────────────────────────────────────────────────────


def execute_bootstrap_job(job_id: str, timeouts: Timeouts | None = None) -> None:
    """Run (or resume) one workspace bootstrap to completion. Never raises."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            return
        workspace_id = (job.payload or {}).get("workspace_id") or job.workspace_id or ""
        job.status = "running"
        db.commit()
        _set_workspace_status(db, workspace_id, STATUS_BOOTSTRAPPING)

        ctx = BootstrapContext(
            workspace_id=workspace_id,
            job_id=job_id,
            workspace=context_for_workspace(workspace_id),
            timeouts=timeouts or Timeouts(),
        )
        done = {
            entry["name"]
            for entry in job_stages(job)
            if entry.get("status") in ("succeeded", "skipped")
        }
        for stage_name in STAGE_ORDER:
            if stage_name in done:
                continue

            def log(msg: str, _stage: str = stage_name) -> None:
                _append_log(db, job_id, _stage, msg)

            ctx.log = log
            _set_stage(db, job_id, stage_name, "running")
            _append_log(db, job_id, stage_name, "stage started")
            try:
                detail = STAGES[stage_name](ctx)
            except Exception as exc:
                # A hard credential-refresh failure (<600s left on the assumed
                # session) can detonate inside ANY stage, not just
                # validate-access — map it to the same actionable diagnostic.
                if isinstance(exc, ClientError) and aws_clients.is_assume_role_failure(exc):
                    detail = aws_clients.assume_role_diagnostic(exc)
                else:
                    detail = f"{type(exc).__name__}: {exc}"
                _set_stage(db, job_id, stage_name, "failed", detail)
                _append_log(db, job_id, stage_name, detail, level="error")
                _append_log(
                    db, job_id, stage_name, traceback.format_exc(limit=3), level="debug"
                )
                _fail(db, job_id, workspace_id, detail)
                return
            _set_stage(db, job_id, stage_name, "succeeded", detail)
            _append_log(db, job_id, stage_name, detail or "succeeded")

        job = db.get(Job, job_id)
        if job is not None:
            job.status = "succeeded"
            db.commit()
    except Exception as exc:  # job-level failure — never crash the worker thread
        db.rollback()
        _fail(db, job_id, None, f"{type(exc).__name__}: {exc}")
    finally:
        db.close()


def _fail(db: Session, job_id: str, workspace_id: str | None, error: str) -> None:
    """Mark the job and its workspace failed, keeping whatever was provisioned.

    The resource map is deliberately left as it is: the identifiers already
    recorded are what makes the retry a resume instead of a second environment.
    """
    job = db.get(Job, job_id)
    if job is not None:
        job.status = "failed"
        job.error = error
        db.commit()
        workspace_id = workspace_id or (job.payload or {}).get("workspace_id")
    if workspace_id:
        _set_workspace_status(db, workspace_id, STATUS_FAILED)


def start_bootstrap_async(job_id: str) -> threading.Thread:
    thread = threading.Thread(
        target=execute_bootstrap_job,
        args=(job_id,),
        name=f"ws-bootstrap-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return thread
