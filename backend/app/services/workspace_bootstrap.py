"""Workspace bootstrap as a resumable job.

`make bootstrap` provisions the hub with CDK plus a linear boto3 pass. A
workspace registered from the console lives in a region where no stack is
deployed and nobody can run `cdk deploy` from a web backend, so the same
substrate is provisioned here with boto3 only, as a staged job:

    validate-access → iam → model-credential → storage → codebuild → gateway → registry →
    observability → finalize

The shape follows `deployer/pipeline.py` on purpose — ordered stages, per-stage
status persisted (here on `Job.payload["stages"]`, the same records the deploy
pipeline keeps on `Deployment.stages`), JSONL events on `Job.log`, and a resume
that skips whatever already succeeded. Every stage is idempotent (list-or-get
then create, the `ensure_*` contract from `bootstrap.py`) and **records the
identifiers it produced on the workspace row as it goes**, so a resumed run sees
the previous run's outputs instead of re-deriving them.

What this deliberately does not provision, and why:

* **memory / knowledge bases** — the substrate is the hub's `launchpad-memory`
  stack; a workspace in the hub's account+region inherits it (`memory` stage),
  any other workspace records `memory_unavailable_reason` (N6, ruling R2). The
  **registry** stage only records `registry_id = local-<workspace_id>` (N4).
* **the demo Build-Tools layer** (hr-database Lambda, office-facts REST API) —
  CDK-shipped sample *code*, so a workspace's gateway simply has no demo targets.

The `model-credential` stage ensures this workspace's region holds the Global
Bedrock API key (spec C4) as an AgentCore Identity API-key credential provider,
read from `LAUNCHPAD_GLOBAL_BEDROCK_API_KEY` and never written or logged — see
`services/model_credential.py`.

A **validate-only** run (`create_bootstrap_job(stages=["validate-access"])`, the route's
`{"validate_only": true}`) executes that one stage and hands the row back in the status it
had — the access verdict a release smoke needs without provisioning anything (r17-ws).

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

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import redacted_error
from app.core.naming import ECR_REPO_BASE, resource_name
from app.models.ledger import Job, Workspace
from app.services import (
    aws_clients,
    bootstrap,
    gateway_bootstrap,
    model_credential,
    observability_bootstrap,
    workspace_iam,
)
from app.services.agent_iam import agent_role_pattern, retry_iam_propagation
from app.services.workspace import (
    WorkspaceContext,
    context_for_workspace,
    merge_workspace_resources,
)

JOB_TYPE = "bootstrap_workspace"

STAGE_ORDER = [
    "validate-access",
    "iam",
    "model-credential",
    "storage",
    "codebuild",
    "gateway",
    "registry",
    "memory",
    "skill-lab",
    "observability",
    "policy",
    "finalize",
]

STATUS_REGISTERED = "registered"
STATUS_BOOTSTRAPPING = "bootstrapping"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

# Job modes (r17-ws, N17-obs117). A FULL run provisions the environment and ends `ready`
# / `failed`. A VALIDATE-ONLY run executes the `validate-access` stage alone — the
# hermetic-free way to prove the hub's credentials can provision a workspace in ITS
# region before anything is created (the release smoke
# `scripts/e2e_workspace_region_smoke.py` runs it after every host update) — creates
# nothing, and hands the workspace row back in the status it had: a verdict, not a
# provisioning step, so it never leaves `bootstrapping` / `ready` / `failed` behind and
# a later full run never treats it as stages already done.
MODE_FULL = "full"
MODE_VALIDATE_ONLY = "validate-only"
VALIDATE_ONLY_STAGES: tuple[str, ...] = ("validate-access",)

# Region-scoped names, identical to the hub's — one workspace per (account,
# region) is a UNIQUE constraint on the table, so no discriminator is needed.
# N11: these are *base* names; `ecr_repo_name()` / `codebuild_project_name()` apply
# `Settings.env_name` and are what every create/lookup below uses.
ECR_REPO_NAME = ECR_REPO_BASE
CODEBUILD_PROJECT_NAME = "launchpad-agent-builder"


def ecr_repo_name() -> str:
    return resource_name(ECR_REPO_NAME)


def codebuild_project_name() -> str:
    return resource_name(CODEBUILD_PROJECT_NAME)


# ARM64 build image + compute matching infra/stacks/base_stack.py:95-100
# (LinuxArmBuildImage.AMAZON_LINUX_2023_STANDARD_3_0 resolves to exactly this
# image id — note the missing "2": amazonlinux2-aarch64-standard:3.0 is the
# Amazon Linux 2 family, a different image).
CODEBUILD_IMAGE = "aws/codebuild/amazonlinux-aarch64-standard:3.0"
CODEBUILD_COMPUTE = "BUILD_GENERAL1_SMALL"
CODEBUILD_TIMEOUT_MINUTES = 30

# Written by `finalize`; a workspace missing any of these cannot deploy or invoke.
REQUIRED_RESOURCE_KEYS = (
    "execution_role_arn",
    "gateway_role_arn",
    "model_credential_arn",
    "artifacts_bucket",
    "ecr_repo",
    "ecr_repo_uri",
    "codebuild_project",
    "gateway_id",
    "gateway_arn",
    "gateway_url",
    "registry_id",
)

_ACCESS_DENIED_CODES = frozenset(
    {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
)
# What AWS answers when a service has no endpoint (or no such operation) in the
# target region. Anything here means "this region cannot host a workspace", which
# is a different operator action than a permissions problem. The last two are
# botocore's own exception classes (`_error_code` names a BotoCoreError by its type),
# raised while RESOLVING the endpoint — before any connection is attempted: no such
# service in the installed botocore's models, no region to resolve against.
_REGION_UNSUPPORTED_CODES = frozenset(
    {
        "UnrecognizedClientException",
        "InvalidClientTokenId",
        "InvalidAction",
        "UnknownOperationException",
        "OptInRequired",
        "UnknownServiceError",
        "NoRegionError",
    }
)
# N17-obs200: botocore raises `EndpointConnectionError` once the endpoint RESOLVED and the
# connect failed (DNS lookup / TCP / TLS to the host) — a network or DNS fault on the hub's
# side, not a statement about the region (a supported region's endpoint during an outage
# raises exactly this). It takes the one bounded retry like a transient and then a DISTINCT
# sentence ("unreachable from this host … network/DNS, not the region"), never the region
# verdict and never the plain "transient — retry" one.
_ENDPOINT_UNREACHABLE_CODES = frozenset({"EndpointConnectionError"})
# N17-obs132: a probe that did not get a usable answer — a transport timeout, a throttle, a
# 5xx — says nothing about the region or the hub's permissions. `_probe` retries such a
# failure ONCE (`Timeouts.probe_attempts` / `probe_retry_delay_s`) and then fails the stage
# as transient, never with the region sentence and never as a denial. Botocore's transport
# classes by type name, the throttling / server-side codes the CN services answer with by
# code (`_is_transient` also reads a ClientError's HTTP status: any 5xx).
_TRANSIENT_CODES = frozenset(
    {
        "ReadTimeoutError",
        "ConnectTimeoutError",
        "Throttling",
        "ThrottlingException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "RequestThrottled",
        "RequestThrottledException",
        "SlowDown",
        "ProvisionedThroughputExceededException",
        "InternalError",
        "InternalFailure",
        "InternalServerError",
        "InternalServerException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
    }
)


class BootstrapError(RuntimeError):
    """A stage failed for a reason the operator can act on."""


class BootstrapConflict(RuntimeError):
    """Another run already owns this workspace's bootstrap."""


class UnsupportedStages(ValueError):
    """A partial run was requested that is not `validate-access` alone.

    Only that subset is meaningful: every later stage records identifiers the following
    ones require, so a run that skips one would leave a resource map the resume logic
    reads as half-provisioned.
    """


ForeignResourceError = workspace_iam.ForeignResourceError


@dataclass
class Timeouts:
    """Waiter budgets, injectable so tests never wait on a stub."""

    gateway_s: int = 300
    iam_attempts: int = 6
    iam_delay_s: int = 10
    # N11-obs11: a whole stage is re-run on a transient AccessDenied (IAM propagation).
    access_denied_attempts: int = bootstrap.ACCESS_DENIED_ATTEMPTS
    access_denied_delay_s: float = bootstrap.ACCESS_DENIED_DELAY_S
    # N17-obs132: one bounded retry of a validate-access probe on a transport / throttle
    # failure (`_TRANSIENT_CODES`) before the stage fails as transient.
    probe_attempts: int = 2
    probe_retry_delay_s: float = 2.0
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


def _new_stages(names: tuple[str, ...] | list[str] | None = None) -> list[dict[str, Any]]:
    return [{"name": name, "status": "pending", "detail": ""} for name in (names or STAGE_ORDER)]


def job_mode(job: Job) -> str:
    """`full` (the default, every pre-r17-ws job) or `validate-only`."""
    return str((job.payload or {}).get("mode") or MODE_FULL)


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


def latest_job(db: Session, workspace_id: str, *, mode: str | None = None) -> Job | None:
    """The newest bootstrap job of the workspace — of one `mode` when given (the mode lives
    in the JSON payload, so the filter runs over the newest rows in Python)."""
    rows = (
        db.query(Job)
        .filter(Job.type == JOB_TYPE, Job.workspace_id == workspace_id)
        .order_by(Job.created_at.desc(), Job.id.desc())
    )
    if mode is None:
        return rows.first()
    for job in rows:
        if job_mode(job) == mode:
            return job
    return None


def create_bootstrap_job(
    db: Session, row: Workspace, *, stages: list[str] | tuple[str, ...] | None = None
) -> Job:
    """Queue a bootstrap run for `row`, carrying forward what already succeeded.

    `stages=None` is a FULL run. `stages=["validate-access"]` is a VALIDATE-ONLY run
    (`MODE_VALIDATE_ONLY`): the one stage, nothing provisioned, the row's status restored
    when the job ends; any other subset raises `UnsupportedStages`. A full run carries
    forward only from the previous FULL job — a validate-only job's green stage is a
    verdict about access, not a provisioned stage.

    A retry after a failure is a resume, not a restart: the stages the previous
    job completed stay marked so the new job skips straight to the first one that
    did not, and so the console shows what is left rather than replaying every
    stage in green.

    Raises `BootstrapConflict` when the workspace is already bootstrapping or
    ready. The claim is a **conditional UPDATE**, not a read-then-write: two
    concurrent requests both pass the router's status check (sync handlers run on
    a threadpool), and two runs against one environment race on every create —
    `create_gateway` would make a second gateway of the same name, the IAM stage a
    second set of roles. Only one of the two UPDATEs can match.
    """
    validate_only = stages is not None
    if validate_only and tuple(stages) != VALIDATE_ONLY_STAGES:
        raise UnsupportedStages(list(stages))
    # read BEFORE the claim: `synchronize_session="fetch"` refreshes the ORM row with it
    previous_status = row.bootstrap_status
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
    if validate_only:
        payload: dict[str, Any] = {
            "workspace_id": row.id,
            "stages": _new_stages(VALIDATE_ONLY_STAGES),
            "mode": MODE_VALIDATE_ONLY,
            "previous_status": previous_status,
        }
    else:
        previous = latest_job(db, row.id, mode=MODE_FULL)
        done = {
            entry["name"]
            for entry in (job_stages(previous) if previous else [])
            if entry.get("status") in ("succeeded", "skipped")
        }
        stage_records = _new_stages()
        for entry in stage_records:
            if entry["name"] in done:
                entry["status"] = "succeeded"
                entry["detail"] = "already provisioned by an earlier run"
        payload = {"workspace_id": row.id, "stages": stage_records, "mode": MODE_FULL}
    job = Job(workspace_id=row.id, type=JOB_TYPE, payload=payload)
    db.add(job)
    db.commit()
    return job


# ── stage: validate-access ─────────────────────────────────────────────────

# One cheap call per service the later stages depend on. Ordered as they are
# needed, so the first failure names the earliest missing capability.
#
# N17-obs130: ECR, CodeBuild and IAM are probed BY NAME against the exact resources the
# later stages create or own (`_stage_storage`'s repository, `_stage_codebuild`'s project,
# `_stage_iam`'s regional execution role) — the shapes the hub's resource-scoped ops policy
# grants (`repository/launchpad-*`, `project/launchpad-*`, `role/launchpad-*`). An unfiltered
# list (`describe_repositories(maxResults=1)`, `list_projects()`, `list_roles(MaxItems=1)`)
# is authorised against the wildcard resource, which no least-privilege statement matches —
# so the stage failed on a least-privilege host in EVERY region, its own included. A
# not-found answer (`_PROBE_NOT_FOUND_CODES`) proves the call reached the service with the
# right permission: the resource simply does not exist yet. The AgentCore control plane and
# S3 have no per-resource shape for their list calls and the policy grants them on `*`.
_PROBES: tuple[tuple[str, str, Callable[[Any, "BootstrapContext"], Any]], ...] = (
    ("bedrock-agentcore-control", "ListGateways",
     lambda c, _ctx: c.list_gateways(maxResults=1)),
    ("s3", "ListBuckets", lambda c, _ctx: c.list_buckets()),
    ("ecr", "DescribeRepositories",
     lambda c, _ctx: c.describe_repositories(repositoryNames=[ecr_repo_name()])),
    ("codebuild", "BatchGetProjects",
     lambda c, _ctx: c.batch_get_projects(names=[codebuild_project_name()])),
    ("iam", "GetRole", lambda c, ctx: c.get_role(RoleName=_execution_role_name(ctx))),
)

# A by-name probe answering "no such resource" has proven the permission: the stage that
# owns the resource will create it.
_PROBE_NOT_FOUND_CODES = frozenset({"RepositoryNotFoundException", "NoSuchEntity"})

# Services whose AccessDenied degrades a feature instead of refusing the bootstrap.
# Empty in the CN build (nothing degrades on AccessDenied; the Registry is local).
_DENIAL_TOLERATED: frozenset[str] = frozenset()


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", ""))
    return type(exc).__name__


def _is_transient(exc: Exception, code: str) -> bool:
    """N17-obs132: a transport timeout, a throttle or a server-side 5xx — the service did
    not refuse and the region did not answer "no such endpoint"; the probe is retried once."""
    if code in _TRANSIENT_CODES:
        return True
    if isinstance(exc, ClientError):
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return isinstance(status, int) and status >= 500
    return False


def _execution_role_name(ctx: BootstrapContext) -> str:
    """The regional execution role `_stage_iam` creates or adopts — probed by name."""
    return workspace_iam.regional_role_name(workspace_iam.EXECUTION_ROLE_BASE, ctx.region)


class _TransientProbeFailure(Exception):
    """Internal: a probe got no usable answer (transport timeout / throttle) — retried once.
    ``unreachable`` marks the N17-obs200 class (the resolved endpoint could not be connected
    to), which gets its own sentence after the retry."""

    def __init__(self, cause: Exception, *, unreachable: bool = False) -> None:
        super().__init__(type(cause).__name__)
        self.cause = cause
        self.unreachable = unreachable


def _probe_once(
    ctx: BootstrapContext,
    service: str,
    operation: str,
    call: Callable[[Any, BootstrapContext], Any],
) -> str:
    try:
        call(ctx.workspace.client(service), ctx)
    except (ClientError, BotoCoreError) as exc:
        if aws_clients.is_assume_role_failure(exc):
            # A credential refresh failed mid-probe: the denial is STS's, not this
            # service's, so reporting it as `<service> <operation> failed` would
            # send the operator after the wrong permission.
            raise BootstrapError(aws_clients.assume_role_diagnostic(exc)) from exc
        code = _error_code(exc)
        if code in _PROBE_NOT_FOUND_CODES:
            ctx.log(f"{service} {operation}: not created yet — the probe's permission is in place")
            return "ok"
        if code in _ACCESS_DENIED_CODES and service in _DENIAL_TOLERATED:
            ctx.log(f"{service} denied by account policy — the feature degrades")
            return "denied"
        if code in _ENDPOINT_UNREACHABLE_CODES:
            # N17-obs200: the endpoint resolved and could not be reached — the hub's
            # network / DNS, not the region; retried once, then its own sentence.
            raise _TransientProbeFailure(exc, unreachable=True) from exc
        if _is_transient(exc, code):
            # N17-obs132: no usable answer is not a verdict — neither on the region nor on
            # the hub's permissions. The remaining BotoCoreErrors (unknown service, no
            # region — raised while resolving the endpoint) ARE the region verdict below;
            # every other ClientError a refusal.
            raise _TransientProbeFailure(exc) from exc
        if code in _REGION_UNSUPPORTED_CODES:
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


def _probe(
    ctx: BootstrapContext,
    service: str,
    operation: str,
    call: Callable[[Any, BootstrapContext], Any],
) -> str:
    """One probe, with ONE bounded retry on a transient failure (N17-obs132). The failure
    text names the service, the operation and the redacted cause (`core.errors.redacted_error`
    — the exception type / code, never botocore's message with the endpoint URL), and says
    "transient — retry": the region sentence is reserved for the endpoint-resolution codes.
    An endpoint that resolved but could not be connected to (N17-obs200) is retried the same
    way and then named as unreachable from this host — network/DNS, not the region; the host
    is redacted to the service and the region (never the URL, never an account id)."""
    attempts = max(1, ctx.timeouts.probe_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return _probe_once(ctx, service, operation, call)
        except _TransientProbeFailure as transient:
            cause = redacted_error(transient.cause)
            label = "endpoint unreachable" if transient.unreachable else "transient failure"
            if attempt < attempts:
                delay = ctx.timeouts.probe_retry_delay_s
                ctx.log(f"{service} {operation}: {label} ({cause}) — retrying once "
                        f"in {delay:g}s")
                ctx.timeouts.sleeper(delay)
                continue
            if transient.unreachable:
                raise BootstrapError(
                    f"{service} endpoint ({service} · {ctx.region}) unreachable from this host "
                    f"after {attempts} attempts ({operation}: {cause}) — network/DNS, not the "
                    "region: check the hub host's egress and DNS resolution for this service "
                    "in this region, then retry the bootstrap"
                ) from transient.cause
            raise BootstrapError(
                f"{service} {operation} failed in {ctx.region} after {attempts} attempts "
                f"({cause}) — transient — retry the bootstrap; neither the region nor the "
                "hub's permissions are in question"
            ) from transient.cause
    raise AssertionError("unreachable")  # pragma: no cover — the loop returns or raises


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


def _project_exists(codebuild: Any, name: str) -> bool:
    return bool(codebuild.batch_get_projects(names=[name]).get("projects"))


def _refuse_foreign_resources(ctx: BootstrapContext) -> None:
    """Refuse a region that already hosts a Launchpad deployment.

    A name match is *not* proof of ownership: this account runs an independent
    production install in another region, whose `launchpad-gw`, CodeBuild project
    and `launchpad-*-role-<region>` would all match. Adopting them would let two
    consoles drive one environment, so the only safe answer is to stop and let the
    operator pick another region.

    A resource this workspace already recorded is its own — that is what makes the
    check re-runnable after a partial bootstrap.
    """
    found: list[str] = []
    control = ctx.workspace.client("bedrock-agentcore-control")
    gateway_name = gateway_bootstrap.gateway_name()
    if not ctx.resources.get("gateway_id") and _find_gateway(control, gateway_name):
        found.append(f"gateway {gateway_name}")
    if not ctx.resources.get("codebuild_project") and _project_exists(
        ctx.workspace.client("codebuild"), codebuild_project_name()
    ):
        found.append(f"CodeBuild project {codebuild_project_name()}")
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
    repo = ctx.resources.get("ecr_repo") or ecr_repo_name()
    return ctx.workspace.arn("ecr", f"repository/{repo}")


def _artifacts_bucket_name(ctx: BootstrapContext) -> str:
    """S3 names are globally unique, and this one already carries account+region —
    the CDK bucket uses exactly this shape."""
    return resource_name(f"launchpad-artifacts-{ctx.account_id}-{ctx.region}")


def _stage_iam(ctx: BootstrapContext) -> str:
    """The two service roles. Runs before `storage` deliberately: an IAM policy
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
            ctx.workspace.service_principal("bedrock-agentcore"), ctx.account_id
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
            ctx.workspace.service_principal("bedrock-agentcore"), ctx.account_id
        ),
        inline_policies={
            "launchpad-gateway": workspace_iam.gateway_role_policy(
                ctx.account_id, ctx.region
            )
        },
        description="Assumed by AgentCore Gateway to reach targets + identity vault",
        log=ctx.log,
    )
    ctx.record(
        {
            "execution_role_arn": execution_role_arn,
            "gateway_role_arn": gateway_role_arn,
        }
    )
    return "2 service roles ready"


# ── stage: model-credential ────────────────────────────────────────────────


def _stage_model_credential(ctx: BootstrapContext) -> str:
    """The Global Bedrock API key as an AgentCore Identity API-key credential provider
    in THIS workspace's region (spec C4). Agents exchange their workload token for it
    (`GetResourceApiKey`); the key itself only ever lives in the operator's shell and
    the Identity vault. Re-runs rotate the key in place."""
    control = ctx.workspace.client("bedrock-agentcore-control")
    try:
        results = model_credential.ensure_provider_credentials(control, log=ctx.log)
    except model_credential.MissingModelCredentialError as exc:
        raise BootstrapError(str(exc)) from exc
    # D21 (M1-b, plan §4): registry providers whose key is an SSM parameter
    # (`credential_source: ssm`) are registered from THIS workspace's `ssm` — the same
    # `launchpad-<name>-api-key` name, expected in the workspace's account/region. A missing
    # parameter is one line (the env pass above keeps its answer), never a stage failure;
    # a registry result replaces the env one only when it configured the provider. Only the
    # hub (default workspace) may stamp the hub-global row's key_version — a spoke keeps the
    # version on its own resource map (D21-obs4).
    # D21-obs5: the stored version this pass compares the parameter against is the hub row's
    # on the hub, and THIS workspace's `resources.model_credentials[<name>].key_version` on a
    # spoke — an unchanged parameter is "up to date": no Identity update, no journal row.
    known = ctx.resources.get("model_credentials")
    registry_results = model_credential.ensure_registry_credentials(
        control, ctx.workspace.client("ssm"), log=ctx.log,
        actor=f"bootstrap:{ctx.workspace_id}",
        record_on_row=ctx.workspace_id == DEFAULT_WORKSPACE_ID,
        known_versions=known if isinstance(known, dict) else None)
    for name, result in registry_results.items():
        if result.get("configured") or name not in results:
            results[name] = result
    ctx.record(model_credential.provider_credential_resources(results))
    parts = []
    for result in results.values():
        if not result.get("configured"):
            parts.append(f"{result['name']} · not configured")
        else:
            suffix = (f" · SSM v{result['key_version']}"
                      if result.get("key_version") is not None else "")
            if result["created"]:
                state = "created"
            elif result.get("rotated", True):
                state = "reused (key rotated)"
            else:
                state = "reused (up to date)"
            parts.append(f"{result['name']} · {state}{suffix}")
    return "; ".join(parts)


# ── stage: storage ─────────────────────────────────────────────────────────


def _ensure_bucket(ctx: BootstrapContext, s3: Any, bucket: str) -> bool:
    """Create-or-adopt, then apply the CDK bucket's posture (idempotent puts)."""
    created = False
    params: dict[str, Any] = {"Bucket": bucket}
    # region-boundary: allow S3 CreateBucket LocationConstraint API quirk
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
                            ctx.workspace.arn("s3", bucket, region="", account=""),
                            ctx.workspace.arn("s3", f"{bucket}/*", region="", account=""),
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
    except ClientError as exc:
        if _error_code(exc) != "RepositoryNotFoundException":
            raise
        repo = ecr.create_repository(
            repositoryName=name,
            # Scan on push so a deploy can be blocked on findings before the image
            # ever backs a runtime.
            imageScanningConfiguration={"scanOnPush": True},
        )["repository"]
        ctx.log(f"created ECR repository {name}")
    return str(repo["repositoryUri"])


def _stage_storage(ctx: BootstrapContext) -> str:
    bucket = _artifacts_bucket_name(ctx)
    _ensure_bucket(ctx, ctx.workspace.client("s3"), bucket)
    repo_uri = _ensure_repository(ctx, ctx.workspace.client("ecr"), ecr_repo_name())
    ctx.record(
        {
            "artifacts_bucket": bucket,
            "ecr_repo": ecr_repo_name(),
            "ecr_repo_uri": repo_uri,
        }
    )
    return f"{bucket} · {ecr_repo_name()}"


# ── stage: codebuild ───────────────────────────────────────────────────────


def _codebuild_env_vars(ctx: BootstrapContext) -> list[dict[str, str]]:
    settings = get_settings()
    # N11-r3-bug7: the project resolves from the BUILD mirror, never the host mirror
    # (`pip_index_url`, tuna) — tuna 4xx-blocks CodeBuild's egress while the hosts get 200.
    build_index = settings.codebuild_pip_index_url
    env_vars = [
        {"name": "PIP_INDEX_URL", "value": build_index, "type": "PLAINTEXT"},
        # uv 0.9.10 deprecates UV_INDEX_URL in favor of UV_DEFAULT_INDEX — set
        # both so the mirror applies regardless of which uv release runs.
        {"name": "UV_INDEX_URL", "value": build_index, "type": "PLAINTEXT"},
        {"name": "UV_DEFAULT_INDEX", "value": build_index, "type": "PLAINTEXT"},
        # the Dockerfiles retry on this one when the primary refuses the build egress
        {"name": "PIP_FALLBACK_INDEX_URL", "value": settings.codebuild_pip_fallback_index_url,
         "type": "PLAINTEXT"},
        {"name": "NPM_CONFIG_REGISTRY", "value": settings.npm_registry, "type": "PLAINTEXT"},
    ]
    if settings.pip_extra_index_url:
        # N11-obs25: optional secondary index for mirror outages (pip + uv spellings)
        env_vars += [
            {"name": "PIP_EXTRA_INDEX_URL", "value": settings.pip_extra_index_url,
             "type": "PLAINTEXT"},
            {"name": "UV_INDEX", "value": settings.pip_extra_index_url, "type": "PLAINTEXT"},
        ]
    if settings.base_image_mirror_enabled:
        env_vars.append({
            "name": "BASE_IMAGE",
            "value": (
                f"{ctx.account_id}.dkr.ecr.{ctx.region}.{ctx.workspace.dns_suffix}/"
                f"{settings.base_image_mirror_prefix}/python:3.13-slim"
            ),
            "type": "PLAINTEXT",
        })
    return env_vars


def _stage_codebuild(ctx: BootstrapContext) -> str:
    bucket = ctx.require("artifacts_bucket")
    role_arn = workspace_iam.ensure_role(
        ctx.workspace.client("iam"),
        workspace_id=ctx.workspace_id,
        role_name=workspace_iam.regional_role_name(
            workspace_iam.CODEBUILD_ROLE_BASE, ctx.region
        ),
        trust_policy=workspace_iam.service_trust_policy(
            ctx.workspace.service_principal("codebuild"), ctx.account_id, source_account=False
        ),
        inline_policies={
            "launchpad-agent-builder": workspace_iam.codebuild_role_policy(
                ctx.account_id,
                ctx.region,
                bucket,
                _ecr_repo_arn(ctx),
                codebuild_project_name(),
            )
        },
        description="Assumed by CodeBuild to build Launchpad agent images",
        log=ctx.log,
    )
    codebuild = ctx.workspace.client("codebuild")
    if _project_exists(codebuild, codebuild_project_name()):
        ctx.log(f"reusing CodeBuild project {codebuild_project_name()}")
    else:
        # Every start_build overrides source and environment variables
        # (agentcore/codebuild.start_image_build), so the placeholder source here
        # only has to be a valid location — same as the CDK project's.
        retry_iam_propagation(
            lambda: codebuild.create_project(
                name=codebuild_project_name(),
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
            ),
            ctx.log,
            attempts=ctx.timeouts.iam_attempts,
            delay_s=ctx.timeouts.iam_delay_s,
            sleeper=ctx.timeouts.sleeper,
        )
        ctx.log(f"created CodeBuild project {codebuild_project_name()}")
    codebuild.update_project(
        name=codebuild_project_name(),
        environment={
            "type": "ARM_CONTAINER",
            "image": CODEBUILD_IMAGE,
            "computeType": CODEBUILD_COMPUTE,
            "privilegedMode": True,
            "environmentVariables": _codebuild_env_vars(ctx),
        },
    )
    ctx.record({"codebuild_project": codebuild_project_name()})
    return codebuild_project_name()


# ── stage: gateway ─────────────────────────────────────────────────────────


def _stage_gateway(ctx: BootstrapContext) -> str:
    """The shared MCP gateway (IAM inbound auth) — no demo targets.

    `hr-database` and `office-facts` are CDK-shipped sample code that only the hub
    has; a workspace's gateway starts empty and gains targets from the Registry.
    """
    control = ctx.workspace.client("bedrock-agentcore-control")
    gateway, created = gateway_bootstrap.ensure_gateway(
        control,
        role_arn=ctx.require("gateway_role_arn"),
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
    return f"{gateway['id']} · no demo targets"


# ── stage: registry ────────────────────────────────────────────────────────


def _stage_registry(ctx: BootstrapContext) -> str:
    """The local Registry (N4) has no AWS footprint: record the identifier that keys
    this workspace's records and system-skill mappings, clear any unavailability
    reason a pre-CN bootstrap may have left, then import the default records
    (N10-bug1): the workspace gateway's targets as APPROVED MCP records — none for a
    spoke, whose gateway starts empty — and the sample skill, submitted for approval.

    The import is best-effort and idempotent: a registry backend that is not
    registered, or a gateway/S3 call that fails, leaves the stage green with the
    reason in its detail (the identifier is what later stages need; the records are
    one ``POST /api/registry/sync-defaults`` away)."""
    from app.services import registry_console
    from app.services.registry_local import registry_id_for

    registry_id = registry_id_for(ctx.workspace)
    values: dict[str, Any] = {"registry_id": registry_id}
    if "registry_unavailable_reason" in ctx.resources:
        values["registry_unavailable_reason"] = ""
    ctx.record(values)
    detail = f"{registry_id} · local ledger"
    try:
        # the workspace bootstrap job (an administrator's) may approve the platform's own
        # targets (N17-obs257)
        results = registry_console.ensure_default_records(
            ctx.workspace, log=ctx.log, auto_approve=True
        )
    except Exception as exc:  # noqa: BLE001 — degrade with the reason, never fail the job
        reason = f"{type(exc).__name__}: {exc}"[:200]
        ctx.log(f"default records not imported — {reason}; POST /api/registry/sync-defaults later")
        return f"{detail} · defaults not imported ({type(exc).__name__})"
    created = sum(1 for r in results if r.get("created"))
    approved = sum(1 for r in results if r.get("status") == "APPROVED")
    return f"{detail} · {len(results)} default record(s) · {created} new · {approved} approved"


# --- CN memory/KB (N6) ---
def _stage_memory(ctx: BootstrapContext) -> str:
    """Self-hosted Memory/KB (N6, ruling R2): the substrate is the hub's launchpad-memory
    stack, so only a workspace in the hub's own account+region can use it (shared domain,
    per-workspace store id). Anywhere else degrades with a reason, like observability."""
    from app.services import memory_bootstrap as mb
    from app.services.memory_selfhosted import store_id_for

    settings = get_settings()
    hub = settings.resources or {}
    if (ctx.account_id, ctx.region) != (
        settings.account_id,
        settings.region,
    ) or not hub.get("memory_os_endpoint"):
        reason = (
            f"self-hosted memory lives in the hub stack ({settings.account_id}/{settings.region}); "
            "this workspace has no OpenSearch domain"
        )
        ctx.record(mb.unavailable(reason))
        ctx.log(reason)
        return "unavailable · hub substrate only"
    resources = {key: hub.get(key, "") for key in mb.MEMORY_RESOURCE_KEYS}
    resources["memory_id"] = store_id_for(ctx.workspace)
    resources["memory_unavailable_reason"] = ""
    admin = WorkspaceContext(
        account_id=ctx.account_id,
        region=ctx.region,
        id=ctx.workspace_id,
        role_arn=resources["memory_admin_role_arn"] or None,
    )
    os_http = aws_clients.opensearch_http(admin, resources["memory_os_endpoint"])
    result = mb.ensure_memory_substrate(
        os_http,
        exec_role_arn=ctx.require("execution_role_arn"),
        extractor_role_arn=resources["memory_extractor_role_arn"],
        agent_role_pattern=ctx.workspace.arn("iam", agent_role_pattern(), region=""),
        log=ctx.log,
    )
    ctx.record(resources)
    return (
        f"{resources['memory_id']} · "
        f"{len(result['created'])} created · {len(result['reused'])} reused"
    )
# --- end CN memory/KB (N6) ---


# ── stage: observability ───────────────────────────────────────────────────


def _ensure_spans_resource_policy(ctx: BootstrapContext) -> None:
    """Workspace-stage wrapper over :func:`ensure_spans_resource_policy`."""
    ensure_spans_resource_policy(ctx.workspace.client("logs"), ctx.workspace, ctx.log)


def ensure_spans_resource_policy(logs: Any, workspace: Any, log: Any = print) -> None:
    """Let X-Ray write into this region's `aws/spans` log group.

    `UpdateTraceSegmentDestination` fails with AccessDenied until a CloudWatch
    Logs RESOURCE policy grants xray.<dns-suffix> PutLogEvents — the hub region
    has one from when Transaction Search was first enabled there, a fresh region
    has none (live-diagnosed on us-east-2, 2026-08-12). Document mirrors the
    hub's `TransactionSearchXRayAccess` policy.
    """
    account = workspace.account_id
    existing = logs.describe_resource_policies().get("resourcePolicies", [])
    if any(p["policyName"] == "TransactionSearchXRayAccess" for p in existing):
        return
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TransactionSearchXRayAccess",
                "Effect": "Allow",
                "Principal": {"Service": workspace.service_principal("xray")},
                "Action": "logs:PutLogEvents",
                "Resource": [
                    workspace.arn("logs", "log-group:aws/spans:*"),
                    workspace.arn("logs", "log-group:/aws/application-signals/data:*"),
                ],
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": account},
                    "ArnLike": {"aws:SourceArn": workspace.arn("xray", "*")},
                },
            }
        ],
    }
    logs.put_resource_policy(
        policyName="TransactionSearchXRayAccess", policyDocument=json.dumps(document)
    )
    log("aws/spans resource policy placed for X-Ray")


def _stage_observability(ctx: BootstrapContext) -> str:
    """Transaction Search, which the hub's `run_bootstrap` also enables.

    It is a per-region X-Ray setting, so a workspace in another region starts with
    it off — and every trace/session/token view reads the `aws/spans` log group it
    populates, i.e. Observability would be silently empty without this.

    Degrades instead of failing, like the pre-CN Registry stage did: an
    environment whose agents deploy and invoke is worth having even where this
    account cannot change the region's trace destination. The reason lands in
    the stage detail and the job log, so it is visible rather than mysterious.
    """
    xray = ctx.workspace.client("xray")
    try:
        _ensure_spans_resource_policy(ctx)
        state = observability_bootstrap.ensure_transaction_search(xray)
    except (ClientError, BotoCoreError) as exc:
        code = _error_code(exc)
        ctx.log(f"Transaction Search unavailable ({code}) — traces will be empty")
        return f"unavailable · {code}"
    if not state.get("enabled"):
        return f"not active yet · {state.get('status') or 'unknown'}"
    return "transaction search active" + ("" if state.get("changed") else " (already)")


# ── stage: policy (D20 P1-d) ───────────────────────────────────────────────


def _stage_policy(ctx: BootstrapContext) -> str:
    """The CloudWatch decisions log group the policy audit sink mirrors this workspace's
    `policy_decisions` into (`/launchpad/policy/decisions[-env]`, in the workspace's own
    account — plan §4.1). Degrades like observability: a workspace that cannot create the
    group still deploys/invokes, decisions stay in the ledger and `GET /api/policies/audit`
    reports the channel `missing`."""
    from app.services import policy_audit

    settings = get_settings()
    if "policy" not in settings.features:
        return "skipped · policy feature off"
    days = int(settings.log_group_retention_days or 0)
    try:
        out = policy_audit.ensure_decision_log_group(
            ctx.workspace.client("logs"), retention_days=days, log=ctx.log,
        )
    except (ClientError, BotoCoreError) as exc:
        code = _error_code(exc)
        ctx.log(
            f"policy decisions log group unavailable ({code}) — decisions stay in the "
            "ledger; the workspace role needs logs:CreateLogGroup + logs:PutRetentionPolicy "
            "+ logs:CreateLogStream + logs:PutLogEvents on log-group:/launchpad/policy/*"
        )
        ctx.record({"policy_log_group": "", "policy_log_group_unavailable_reason": code})
        return f"unavailable · {code}"
    ctx.record({"policy_log_group": out["log_group"], "policy_log_group_unavailable_reason": ""})
    state = "created" if out["created"] else "exists"
    retention = f"retention {days} d" if days > 0 else "retention unbounded"
    return f"decisions log group {out['log_group']} {state} · {retention}"


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
    "model-credential": _stage_model_credential,
    "storage": _stage_storage,
    "codebuild": _stage_codebuild,
    "gateway": _stage_gateway,
    "registry": _stage_registry,
    "memory": _stage_memory,
    "skill-lab": _stage_skill_lab,
    "observability": _stage_observability,
    "policy": _stage_policy,
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
        records = job_stages(job)
        done = {
            entry["name"]
            for entry in records
            if entry.get("status") in ("succeeded", "skipped")
        }
        # the job's own stage list: every stage for a full run, `validate-access` alone for a
        # validate-only run (a pre-r17-ws job without `mode` has the full list)
        requested = [entry["name"] for entry in records] or list(STAGE_ORDER)
        validate_only = job_mode(job) == MODE_VALIDATE_ONLY
        restore = str((job.payload or {}).get("previous_status") or STATUS_REGISTERED)
        for stage_name in STAGE_ORDER:
            if stage_name in done or stage_name not in requested:
                continue

            def log(msg: str, _stage: str = stage_name) -> None:
                _append_log(db, job_id, _stage, msg)

            ctx.log = log
            _set_stage(db, job_id, stage_name, "running")
            _append_log(db, job_id, stage_name, "stage started")
            try:
                # N11-obs11: a transient AccessDenied right after an IAM change re-runs
                # the (idempotent) stage instead of failing the job; the retry line
                # lands in the job log like any other stage message.
                detail = bootstrap.retry_access_denied(
                    lambda _stage=stage_name: STAGES[_stage](ctx),
                    label=f"stage {stage_name}",
                    attempts=ctx.timeouts.access_denied_attempts,
                    delay_s=ctx.timeouts.access_denied_delay_s,
                    sleeper=ctx.timeouts.sleeper,
                    log=log,
                )
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
                _fail(db, job_id, workspace_id, detail,
                      restore_status=restore if validate_only else None)
                return
            _set_stage(db, job_id, stage_name, "succeeded", detail)
            _append_log(db, job_id, stage_name, detail or "succeeded")

        job = db.get(Job, job_id)
        if job is not None:
            job.status = "succeeded"
            db.commit()
        if validate_only:
            # a verdict, not a provisioning step: the row goes back to what it was
            _set_workspace_status(db, workspace_id, restore)
    except Exception as exc:  # job-level failure — never crash the worker thread
        db.rollback()
        _fail(db, job_id, None, f"{type(exc).__name__}: {exc}")
    finally:
        db.close()


def _fail(
    db: Session,
    job_id: str,
    workspace_id: str | None,
    error: str,
    *,
    restore_status: str | None = None,
) -> None:
    """Mark the job failed and the workspace `failed` — or, for a validate-only run,
    hand the workspace back the status it had (`restore_status`): nothing was attempted,
    so there is nothing to resume. The resource map is deliberately left as it is: the
    identifiers already recorded are what makes the retry a resume instead of a second
    environment.
    """
    job = db.get(Job, job_id)
    if job is not None:
        job.status = "failed"
        job.error = error
        db.commit()
        workspace_id = workspace_id or (job.payload or {}).get("workspace_id")
        if restore_status is None and job_mode(job) == MODE_VALIDATE_ONLY:
            restore_status = str((job.payload or {}).get("previous_status") or STATUS_REGISTERED)
    if workspace_id:
        _set_workspace_status(db, workspace_id, restore_status or STATUS_FAILED)


INTERRUPTED_PROBE_ERROR = (
    "interrupted by a backend restart before the probe finished — the access check was not "
    "completed; run it again"
)


def sweep_interrupted(db: Session) -> dict[str, list[str]]:
    """Startup (rev-r17 nit on cn/r17-q): undo what a dead process left behind so no workspace
    stays `bootstrapping` with nothing alive to finish it — every later bootstrap / smoke would
    answer `409 workspace.bootstrap_conflict` forever.

    * a VALIDATE-ONLY job still `queued` / `running` is a probe nobody will finish: mark it
      `failed` with `INTERRUPTED_PROBE_ERROR` (the running stage `failed` too) and hand the row
      back the status it had — a probe is a verdict, re-running it is the operator's call;
    * a FULL job still `queued` / `running` is left for `resume_pending_jobs` (the stages are
      idempotent and the resume adopts what the interrupted run made) — reported here;
    * a row `bootstrapping` with NO live bootstrap job (the process died between the job's
      terminal commit and the row's status write, or the job row is gone) is restored from its
      newest job: a validate-only job → its `previous_status`; a full job → `failed` / `ready`
      by its terminal status; no job at all → `registered`.

    Returns ``{"probes_failed": [job ids], "full_resumable": [job ids], "rows_restored":
    [workspace ids]}`` for the startup log. Never raises.
    """
    out: dict[str, list[str]] = {"probes_failed": [], "full_resumable": [], "rows_restored": []}
    live = (
        db.query(Job)
        .filter(Job.type == JOB_TYPE, Job.status.in_(["queued", "running"]))
        .all()
    )
    still_live: set[str] = set()
    for job in live:
        workspace_id = str((job.payload or {}).get("workspace_id") or job.workspace_id or "")
        if job_mode(job) != MODE_VALIDATE_ONLY:
            out["full_resumable"].append(job.id)
            still_live.add(workspace_id)
            continue
        payload = dict(job.payload or {})
        stages = [dict(entry) for entry in payload.get("stages") or []]
        for entry in stages:
            if entry.get("status") in ("running", "pending"):
                entry["status"] = "failed"
                entry["detail"] = INTERRUPTED_PROBE_ERROR
                entry["ended_at"] = _now_iso()
        payload["stages"] = stages
        job.payload = payload
        job.status = "failed"
        job.error = INTERRUPTED_PROBE_ERROR
        line = json.dumps({"ts": _now_iso(), "stage": "validate-access", "level": "error",
                           "msg": INTERRUPTED_PROBE_ERROR}, ensure_ascii=False)
        job.log = (job.log + "\n" + line) if job.log else line
        restore = str(payload.get("previous_status") or STATUS_REGISTERED)
        row = db.get(Workspace, workspace_id) if workspace_id else None
        if row is not None and row.bootstrap_status == STATUS_BOOTSTRAPPING:
            row.bootstrap_status = restore
        out["probes_failed"].append(job.id)
    db.commit()
    stuck = (
        db.query(Workspace)
        .filter(Workspace.bootstrap_status == STATUS_BOOTSTRAPPING)
        .all()
    )
    for row in stuck:
        if row.id in still_live:
            continue
        newest = latest_job(db, row.id)
        if newest is None:
            restore = STATUS_REGISTERED
        elif job_mode(newest) == MODE_VALIDATE_ONLY:
            restore = str((newest.payload or {}).get("previous_status") or STATUS_REGISTERED)
        elif newest.status == "succeeded":
            restore = STATUS_READY
        else:
            restore = STATUS_FAILED
        row.bootstrap_status = restore
        out["rows_restored"].append(row.id)
    db.commit()
    return out


def start_bootstrap_async(job_id: str) -> threading.Thread:
    thread = threading.Thread(
        target=execute_bootstrap_job,
        args=(job_id,),
        name=f"ws-bootstrap-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return thread
