"""Phase 2 isolation, IAM (projects design §10.3, R10-iam): the project permissions
boundary and the provisioner of `project_provision`'s `boundary` stage.

Every isolated project gets one customer managed policy per workspace,
`launchpad-project-<pid>[-<region>]` (`workspace_iam.project_boundary_policy_name`),
attached as the **permissions boundary** of each of its agents' execution roles. The
per-agent identity policy (`agent_iam.policy_document`) stays what it is — least
privilege derived from the spec — and the boundary is the ceiling over it: whatever a
spec-generator defect grants, an agent cannot reach outside its project.

Why per project rather than the design's single workspace-wide ABAC policy keyed on
`${aws:PrincipalTag/launchpad:project}`: two rows of the §10.3 table need explicit,
project-specific ARNs anyway (the project's Connections, which carry no project prefix,
and the explicit KB list that stands in until KB tag conditions are verified), and a
shared document cannot carry them. With a per-project document the project id is a
literal, so a role whose tag was rewritten still cannot cross over. The cost is one
managed policy per (project, workspace) — §10.12's quota row; the preflight counts it.

Mechanisms per capability (the design's table; "TO VERIFY" marks the service support
the design lists as unconfirmed — each has a fallback noted where it is used):

* tag      — `aws:ResourceTag/launchpad:project = <pid>` on the resource
* prefix   — the project id is part of the resource name (memory, S3, ECR, M2M provider)
* explicit — exact ARNs, collected from the project's agents (KBs, Connections)

Legacy shared resources (the workspace memory, the shared gateway M2M provider, the KB
gateway's provider) stay inside the boundary until `settings.isolation.legacy_retired`
is set by §10.11's `retire` stage (R10-migrate): agents keep working through the
grant-new → boundary → cutover sequence (§10.16 "先加后减").

The boundary is versioned: a change (a new Connection, a KB attached) writes a new
default version, `vN`, and an agent's role records the one it carries as
`agents.isolation_applied = boundary-v<N>` for drift detection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.core.regions import partition_for_region
from app.models.project_isolation import (
    KIND_BOUNDARY_POLICY,
    KIND_INFERENCE_PROFILE_PREFIX,
    STATUS_READY,
    ProjectResource,
)
from app.services import project_isolation as isolation
from app.services.project_telemetry import SHARED_PROJECT
from app.services.workspace_iam import (
    WORKSPACE_TAG_KEY,
    ForeignResourceError,
    project_boundary_policy_name,
)

logger = logging.getLogger(__name__)

PROJECT_TAG_KEY = "launchpad:project"
# The spoke template's CreateRole Deny keys on this request tag: a role tagged
# isolated cannot be created without a `launchpad-project-*` boundary.
ISOLATION_TAG_KEY = "launchpad:isolation"
ISOLATED_TAG_VALUE = "isolated"
# platform-owned resources an isolated agent may still use (the platform gateway)
PLATFORM_PROJECT = "platform"

# IAM limits: a managed policy document is at most 6,144 characters (whitespace not
# counted) and keeps at most five versions.
MAX_POLICY_CHARS = 6144
MAX_POLICY_VERSIONS = 5

# The shared gateway M2M provider (gateway_bootstrap.py) — legacy inside the boundary.
SHARED_M2M_PROVIDER = "launchpad-gw-m2m"


# ── inputs ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BoundaryInputs:
    """Everything the boundary document depends on. Pure data, so
    `boundary_policy_document` stays a pure function (like `agent_iam.policy_document`)."""

    project_id: str
    account_id: str
    region: str
    artifacts_bucket: str = ""
    shared_ecr_repo: str = "launchpad-agents"
    # legacy shared resources (§10.11): kept until `retire`
    legacy_memory_id: str = ""
    kb_gateway_provider_arn: str = ""
    include_legacy: bool = True
    # explicit lists, collected from the project's agents (sorted, deduped)
    kb_ids: tuple[str, ...] = ()
    oauth2_connections: tuple[str, ...] = ()
    api_key_connections: tuple[str, ...] = ()
    # §10.8: once the project has application inference profiles, system profiles go
    application_inference_profiles: bool = False
    # the workspace's agent-files bucket, set only when a project agent has a session
    # output folder (`filesystem.outputs`): its mount runs under this boundary too
    agent_files_bucket: str = ""

    @property
    def partition(self) -> str:
        return partition_for_region(self.region)

    @property
    def memory_name(self) -> str:
        return isolation.resource_names(self.project_id).memory

    @property
    def m2m_provider(self) -> str:
        return isolation.resource_names(self.project_id).m2m_provider


# Other modules extend the boundary with the statements their own grants need (the
# per-agent counterpart is `agent_iam.EXTRA_STATEMENT_BUILDERS`): a grant added to the
# identity policy but not here is denied by the boundary at invoke time. Each builder
# is called with the inputs and returns statements to append (or none).
EXTRA_BOUNDARY_STATEMENT_BUILDERS: list[Callable[[BoundaryInputs], list[dict[str, Any]]]] = []


def _shared_kb_statements(inp: BoundaryInputs) -> list[dict[str, Any]]:
    """§11.2: read access to the KBs other projects shared with this one (`use`)."""
    from app.services import resource_shares

    return resource_shares.boundary_statements(inp)


EXTRA_BOUNDARY_STATEMENT_BUILDERS.append(_shared_kb_statements)


def _storage_bucket_statements(inp: BoundaryInputs) -> list[dict[str, Any]]:
    """S3 bucket entries allowed to the project (the mount runs under the boundary,
    probe P9, 2026-11-23)."""
    from app.services import storage_mounts

    return storage_mounts.boundary_statements(inp)


EXTRA_BOUNDARY_STATEMENT_BUILDERS.append(_storage_bucket_statements)


# ── the document ──────────────────────────────────────────────────────────────


def _tagged(project_ids: str | list[str], *, if_exists: bool = False) -> dict[str, Any]:
    op = "StringEqualsIfExists" if if_exists else "StringEquals"
    return {op: {f"aws:ResourceTag/{PROJECT_TAG_KEY}": project_ids}}


def _vault_secret(inp: BoundaryInputs, kind: str, name: str) -> str:
    # The token vault stores a provider's secret as
    # `bedrock-agentcore-identity!default/<kind>/<name>` and Secrets Manager appends
    # `-` + six random characters. `-??????` pins exactly that suffix: `-*` would let
    # `launchpad-gw-m2m` match `launchpad-gw-m2m-<another project>-…`.
    return (f"arn:{inp.partition}:secretsmanager:{inp.region}:{inp.account_id}:secret:"
            f"bedrock-agentcore-identity!default/{kind}/{name}-??????")


def boundary_policy_document(inp: BoundaryInputs) -> dict[str, Any]:
    """The project's permissions boundary in one workspace (design §10.3 table, one
    block per row). Every action a per-agent identity policy can grant appears here,
    so the boundary never denies what an agent of this project legitimately does —
    `tests/test_project_iam.py` checks that against `agent_iam.policy_document`.

    Written compactly (action wildcards, merged statements, short sids) because a
    managed policy is capped at `MAX_POLICY_CHARS` and the explicit lists grow with the
    project; the identity policy is what stays exact."""
    p, region, acct, pid = inp.partition, inp.region, inp.account_id, inp.project_id
    agentcore = f"arn:{p}:bedrock-agentcore:{region}:{acct}"
    statements: list[dict[str, Any]] = []

    # ---- models ----
    model_actions = ["bedrock:Converse*", "bedrock:InvokeModel*"]
    model_resources = [f"arn:{p}:bedrock:*::foundation-model/*"]
    if not inp.application_inference_profiles:
        # system (cross-region) profiles carry no project tag; the region segment is
        # wildcarded like agent_iam's BYOC ARNs
        model_resources.append(f"arn:{p}:bedrock:*:{acct}:inference-profile/*")
    statements.append({"Sid": "Models", "Effect": "Allow", "Action": model_actions,
                       "Resource": model_resources})

    # ---- everything that authorizes on the project tag ----
    # One statement for every tag-conditioned row of the table: the resource is `*`
    # because the condition *is* the scope (ABAC), and an action never meets another
    # service's resource in a real request, so merging them grants nothing extra.
    statements.append({
        "Sid": "ProjectTagged", "Effect": "Allow",
        "Action": [
            # models (§10.8): application inference profiles of the project.
            # TO VERIFY: Bedrock authorizes them on aws:ResourceTag. Fallback: the
            # profile ARNs recorded as `inference_profile:<model>` rows, listed.
            *model_actions,
            # KBs (R10-kb tags them). TO VERIFY: Bedrock KB honours aws:ResourceTag.
            # Fallback: KbListed below.
            "bedrock:GetKnowledgeBase", "bedrock:Retrieve*",
            # A2A peers. TO VERIFY: InvokeAgentRuntime honours aws:ResourceTag;
            # runtimes are tagged at create (R10-obs) and back-filled (§10.8
            # tag-backfill). Untagged peers are denied — cross-project A2A is phase
            # 3's sharing grant (§11.2).
            "bedrock-agentcore:InvokeAgentRuntime",
            # agents-as-tools with runtime_user (SB-2) name the end user
            "bedrock-agentcore:InvokeAgentRuntimeForUser",
            # ECR: a BYOC container_image may name any repository of the account;
            # allowed when it is tagged for the project (ECR supports aws:ResourceTag)
            "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
            # secrets. TO VERIFY: the token vault's own secrets can carry the tag;
            # until then they match VaultSecrets by name.
            "secretsmanager:GetSecretValue",
        ],
        "Resource": "*",
        "Condition": _tagged(pid),
    })
    statements.append({
        # Mantle's third-party families subscribe through Marketplace, only as called
        # by Mantle (the identity policy's own condition)
        "Sid": "MantleMarketplace", "Effect": "Allow",
        "Action": ["aws-marketplace:Subscribe", "aws-marketplace:ViewSubscriptions"],
        "Resource": "*",
        "Condition": {"StringEquals": {"aws:CalledViaLast": "bedrock-mantle.amazonaws.com"}},
    })

    # ---- memory: the project memory by name prefix ----
    # Memory ids are `<name>-<suffix>` and names cannot contain `-`, so
    # `launchpad_mem_<pid>-*` cannot match another project's memory.
    # TO VERIFY: the memory id keeps the name as its prefix (it did for the workspace
    # memory). Fallback: the id R10-mem records on the `memory` row, listed explicitly.
    memory_resources = [f"{agentcore}:memory/{inp.memory_name}-*"]
    if inp.include_legacy and inp.legacy_memory_id:
        # the shared workspace memory, until §10.4's dual-read period ends
        memory_resources.append(f"{agentcore}:memory/{inp.legacy_memory_id}")
    statements.append({
        "Sid": "Memory", "Effect": "Allow",
        "Action": ["bedrock-agentcore:*Event*", "bedrock-agentcore:*MemoryRecord*",
                   "bedrock-agentcore:ListActors", "bedrock-agentcore:ListSessions"],
        "Resource": memory_resources,
    })

    # ---- knowledge bases ----
    # ProjectTagged above covers the tagged ones
    kb_actions = ["bedrock:GetKnowledgeBase", "bedrock:Retrieve*"]
    kb = f"arn:{p}:bedrock:{region}:{acct}:knowledge-base"
    if inp.kb_ids:
        statements.append({"Sid": "KbListed", "Effect": "Allow", "Action": kb_actions,
                           "Resource": [f"{kb}/{kb_id}" for kb_id in inp.kb_ids]})
    statements.append({
        # AgenticRetrieveStream authorizes on `*` (agent_iam's UNSCOPABLE note). The
        # design adds the tag condition; IfExists keeps today's behaviour while the
        # request carries no resource tag. TO VERIFY: whether the service evaluates
        # a KB tag on it at all — if it does, this narrows to the project's KBs.
        "Sid": "KbAgentic", "Effect": "Allow",
        "Action": ["bedrock:AgenticRetrieveStream"], "Resource": "*",
        "Condition": _tagged(pid, if_exists=True),
    })

    # ---- S3: the project prefix, platform skills, legacy agent keys — read-only ----
    if inp.artifacts_bucket:
        bucket = f"arn:{p}:s3:::{inp.artifacts_bucket}"
        prefixes = [f"projects/{pid}/", "skills/", "agents/"]
        statements.append({"Sid": "S3Read", "Effect": "Allow", "Action": ["s3:GetObject"],
                           "Resource": [f"{bucket}/{prefix}*" for prefix in prefixes]})
        statements.append({
            "Sid": "S3List", "Effect": "Allow", "Action": ["s3:ListBucket"],
            "Resource": [bucket],
            "Condition": {"StringLike": {"s3:prefix": [f"{prefix}*" for prefix in prefixes]}},
        })

    # ---- session output folder: the project's own prefix of the agent-files bucket;
    # the per-agent role policy narrows it to the agent's own sessions ----
    if inp.agent_files_bucket:
        files = f"arn:{p}:s3:::{inp.agent_files_bucket}"
        project_prefix = f"projects/{pid}/"
        statements.append({
            "Sid": "AgentFilesObjects", "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload",
                       "s3:DeleteObject"],
            "Resource": [f"{files}/{project_prefix}*"],
        })
        statements.append({
            "Sid": "AgentFilesList", "Effect": "Allow", "Action": ["s3:ListBucket"],
            "Resource": [files],
            "Condition": {"StringLike": {"s3:prefix": [f"{project_prefix}*"]}},
        })

    # ---- ECR: the project repository and the legacy shared one (pull only); tagged
    # repositories: ProjectTagged ----
    repo = f"arn:{p}:ecr:{region}:{acct}:repository"
    ecr_pull = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    statements.append({
        "Sid": "Ecr", "Effect": "Allow", "Action": ecr_pull,
        "Resource": [f"{repo}/{inp.shared_ecr_repo}/{pid}", f"{repo}/{inp.shared_ecr_repo}"],
    })

    # ---- gateways (SigV4): the project's, or the platform's / workspace's shared ----
    statements.append({
        # TO VERIFY: InvokeGateway honours aws:ResourceTag (R10-gw / R10-obs tag
        # gateways). OAuth gateways authorize the bearer token, not this grant —
        # those are limited by §10.6's per-project M2M client and Cedar.
        "Sid": "Gateways", "Effect": "Allow", "Action": ["bedrock-agentcore:InvokeGateway"],
        "Resource": [f"arn:{p}:bedrock-agentcore:*:{acct}:gateway/*"],
        # the workspace gateway is tagged launchpad:project=shared (cost-tags §1.2)
        "Condition": _tagged([pid, PLATFORM_PROJECT, SHARED_PROJECT]),
    })

    # ---- identity: workload tokens, then the project's providers by ARN ----
    vault = f"{agentcore}:token-vault/default"
    statements.append({
        # the per-agent identity policy pins the agent's OWN workload identity; the
        # boundary cannot name each agent, so it allows the directory. A token call
        # also authorizes on the provider, which only the next two statements allow.
        "Sid": "Identity", "Effect": "Allow",
        "Action": ["bedrock-agentcore:GetResourceApiKey",
                   "bedrock-agentcore:GetResourceOauth2Token",
                   "bedrock-agentcore:GetWorkloadAccessToken*"],
        "Resource": [vault, f"{agentcore}:workload-identity-directory/default",
                     f"{agentcore}:workload-identity-directory/default/workload-identity/*"],
    })
    oauth2 = [inp.m2m_provider, *inp.oauth2_connections]
    if inp.include_legacy:
        oauth2.append(SHARED_M2M_PROVIDER)
    oauth2 = sorted(set(oauth2))
    oauth2_arns = [f"{vault}/oauth2credentialprovider/{name}" for name in oauth2]
    if inp.include_legacy and inp.kb_gateway_provider_arn:
        oauth2_arns.append(inp.kb_gateway_provider_arn)
    statements.append({"Sid": "Oauth2Providers", "Effect": "Allow",
                       "Action": ["bedrock-agentcore:GetResourceOauth2Token"],
                       "Resource": sorted(set(oauth2_arns))})
    if inp.api_key_connections:
        statements.append({
            "Sid": "ApiKeyProviders", "Effect": "Allow",
            "Action": ["bedrock-agentcore:GetResourceApiKey"],
            "Resource": [f"{vault}/apikeycredentialprovider/{name}"
                         for name in inp.api_key_connections],
        })

    # ---- secrets: the vault secrets of the project's providers (tagged ones: above) ----
    secret_names = [_vault_secret(inp, "oauth2", name) for name in oauth2]
    if inp.include_legacy and inp.kb_gateway_provider_arn:
        secret_names.append(_vault_secret(inp, "oauth2",
                                          inp.kb_gateway_provider_arn.rsplit("/", 1)[-1]))
    secret_names += [_vault_secret(inp, "apikey", name) for name in inp.api_key_connections]
    statements.append({"Sid": "VaultSecrets", "Effect": "Allow",
                       "Action": ["secretsmanager:GetSecretValue"],
                       "Resource": sorted(set(secret_names))})

    # ---- workspace-wide, as today (nothing project-shaped to scope on) ----
    statements.append({
        # * Mantle: its own IAM service; projects there are not Launchpad projects.
        # * Code interpreter / browser sessions: `*` as in the identity policy; phase
        #   2's VPC track gives a project its own interpreter (§12).
        # * Configuration bundles are named per experiment and untagged — TO VERIFY
        #   tag support before narrowing.
        # * BYO file-system mounts are customer resources with no project tag; the
        #   identity policy pins the access points and inline mounts are admin-only.
        # * ECR's token and X-Ray ingestion take no resource.
        "Sid": "WorkspaceWide", "Effect": "Allow",
        "Action": [
            "bedrock-agentcore:*BrowserSession",
            "bedrock-agentcore:*CodeInterpreterSession",
            "bedrock-agentcore:ConnectBrowser*",
            # the read-only halves of the builtin tool grants (agent_iam)
            "bedrock-agentcore:GetBrowser",
            "bedrock-agentcore:GetCodeInterpreter",
            "bedrock-agentcore:ListBrowserSessions",
            "bedrock-agentcore:ListBrowsers",
            "bedrock-agentcore:ListCodeInterpreterSessions",
            "bedrock-agentcore:ListCodeInterpreters",
            "bedrock-agentcore:UpdateBrowserStream",
            "bedrock-agentcore:GetConfigurationBundle*",
            "bedrock-agentcore:InvokeCodeInterpreter",
            "bedrock-mantle:*",
            "ecr:GetAuthorizationToken",
            "elasticfilesystem:Client*",
            "s3files:Client*",
            "s3files:GetAccessPoint",
            "s3files:ListMountTargets",
            "xray:GetSampling*",
            "xray:Put*",
        ],
        "Resource": "*",
    })
    statements.append({
        # §10.16: an agent can still write another runtime's log group (integrity,
        # not confidentiality) — accepted and documented. `*` covers the streams.
        "Sid": "Logs", "Effect": "Allow",
        "Action": ["logs:CreateLog*", "logs:DescribeLogStreams", "logs:PutLogEvents"],
        "Resource": [f"arn:{p}:logs:{region}:{acct}:log-group:/aws/bedrock-agentcore/runtimes/*"],
    })
    statements.append({
        "Sid": "Metrics", "Effect": "Allow", "Action": ["cloudwatch:PutMetricData"],
        "Resource": "*",
        "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
    })

    for build in EXTRA_BOUNDARY_STATEMENT_BUILDERS:
        statements.extend(build(inp) or [])
    return {"Version": "2012-10-17", "Statement": statements}


def policy_chars(document: dict[str, Any]) -> int:
    """The size IAM counts against `MAX_POLICY_CHARS` (whitespace excluded)."""
    return len(json.dumps(document, separators=(",", ":")))


def digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()[:16]


# ── what the project's agents need (the explicit lists) ───────────────────────


@dataclass
class _Needs:
    kb_ids: set[str] = field(default_factory=set)
    oauth2: set[str] = field(default_factory=set)
    api_key: set[str] = field(default_factory=set)
    outputs: bool = False

    def add_spec(self, spec: Any) -> None:
        if getattr(getattr(spec, "filesystem", None), "outputs", None) is not None:
            self.outputs = True
        for kb in getattr(spec, "knowledge_bases", None) or []:
            self.kb_ids.add(kb.kb_id)
        for tool in getattr(spec, "tools", None) or []:
            auth = getattr(tool, "auth", None)
            if auth is None:
                continue
            (self.oauth2 if auth.kind == "oauth2" else self.api_key).add(auth.connection)


def _agent_needs(project_id: str, workspace_id: str) -> _Needs:
    """KB ids and Connections used by the project's live agents in the workspace."""
    from app.models.ledger import Agent
    from app.schemas.agent import AgentSpec

    needs = _Needs()
    db = SessionLocal()
    try:
        with project_context(project_id):
            rows = db.scalars(select(Agent).where(
                Agent.project_id == project_id, Agent.workspace_id == workspace_id,
                Agent.status != "deleted",
            ))
            for row in rows:
                try:
                    needs.add_spec(AgentSpec(**(row.spec or {})))
                except Exception:  # noqa: BLE001 — a malformed stored spec grants nothing
                    logger.warning("agent %s: spec unreadable for the boundary", row.id)
    finally:
        db.close()
    return needs


def _project_flags(project_id: str, workspace_id: str) -> tuple[bool, bool]:
    """(legacy retired, application inference profiles present)."""
    from app.models.projects import Project

    db = SessionLocal()
    try:
        row = db.get(Project, project_id)
        raw = ((row.settings or {}).get("isolation") if row is not None else None) or {}
        retired = bool(raw.get("legacy_retired")) if isinstance(raw, dict) else False
        profiles = db.scalar(select(ProjectResource.kind).where(
            ProjectResource.project_id == project_id,
            ProjectResource.workspace_id == workspace_id,
            ProjectResource.kind.startswith(KIND_INFERENCE_PROFILE_PREFIX),
        ).limit(1)) is not None
        return retired, profiles
    finally:
        db.close()


def inputs_for(project_id: str, workspace: Any, *, extra_specs: Iterable[Any] = ()
               ) -> BoundaryInputs:
    """The boundary inputs of (project, workspace) from the ledger — plus `extra_specs`,
    the spec being deployed right now (it may not be the stored one yet)."""
    resources = workspace.resources or {}
    needs = _agent_needs(project_id, workspace.id)
    for spec in extra_specs:
        needs.add_spec(spec)
    retired, profiles = _project_flags(project_id, workspace.id)
    return BoundaryInputs(
        project_id=project_id,
        account_id=workspace.account_id,
        region=workspace.region,
        artifacts_bucket=resources.get("artifacts_bucket", ""),
        shared_ecr_repo=resources.get("ecr_repo", "launchpad-agents"),
        legacy_memory_id=resources.get("memory_id", ""),
        kb_gateway_provider_arn=resources.get("oauth_provider_arn", ""),
        include_legacy=not retired,
        kb_ids=tuple(sorted(needs.kb_ids)),
        oauth2_connections=tuple(sorted(needs.oauth2)),
        api_key_connections=tuple(sorted(needs.api_key)),
        application_inference_profiles=profiles,
        agent_files_bucket=(resources.get("agent_files_bucket", "") if needs.outputs else ""),
    )


# ── the IAM writes ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BoundaryState:
    name: str
    arn: str
    version: int  # N of the default version `vN`
    changed: bool  # a version (or the policy) was written by this call
    digest: str

    @property
    def label(self) -> str:
        """`agents.isolation_applied`."""
        return f"boundary-v{self.version}"


def _version_number(version_id: str) -> int:
    try:
        return int(str(version_id).lstrip("vV"))
    except ValueError:
        return 0


def _document_of(raw: Any) -> dict[str, Any]:
    # boto3 decodes the URL-encoded JSON for get_policy_version; a raw string (other
    # SDK paths, recorded fixtures) is decoded here
    if isinstance(raw, dict):
        return raw
    return json.loads(urllib.parse.unquote(str(raw)))


def _is_no_such_entity(exc: Exception) -> bool:
    return "NoSuchEntity" in f"{type(exc).__name__}{exc}"


def _tags(project_id: str, workspace_id: str) -> list[dict[str, str]]:
    return [
        {"Key": PROJECT_TAG_KEY, "Value": project_id},
        {"Key": WORKSPACE_TAG_KEY, "Value": workspace_id},
        {"Key": "launchpad:managed", "Value": "true"},
    ]


def ensure_boundary_policy(iam: Any, inp: BoundaryInputs, workspace_id: str,
                           log: Callable[[str], None] = lambda _m: None) -> BoundaryState:
    """Create the boundary policy, or make `document` its default version. Idempotent:
    an unchanged document writes nothing. A policy of the right name that is not
    tagged for this project is refused, as `workspace_iam.ensure_role` refuses roles."""
    document = boundary_policy_document(inp)
    size = policy_chars(document)
    if size > MAX_POLICY_CHARS:
        raise isolation.ProvisionError(
            f"the project boundary is {size} characters, over IAM's {MAX_POLICY_CHARS} "
            f"— too many knowledge bases or Connections listed explicitly "
            f"({len(inp.kb_ids)} KBs, {len(inp.oauth2_connections) + len(inp.api_key_connections)} "
            "Connections)"
        )
    name = project_boundary_policy_name(inp.project_id, inp.region)
    arn = f"arn:{inp.partition}:iam::{inp.account_id}:policy/{name}"
    body = json.dumps(document)
    want = digest(document)
    try:
        policy = iam.get_policy(PolicyArn=arn)["Policy"]
    except Exception as exc:  # noqa: BLE001 — the SDK's typed error is client-specific
        if not _is_no_such_entity(exc):
            raise
        created = iam.create_policy(
            PolicyName=name,
            PolicyDocument=body,
            Description=(f"Launchpad permissions boundary for project {inp.project_id} "
                         f"in workspace {workspace_id}"),
            Tags=_tags(inp.project_id, workspace_id),
        )["Policy"]
        log(f"created boundary policy {name}")
        return BoundaryState(name, created.get("Arn", arn),
                             _version_number(created.get("DefaultVersionId", "v1")) or 1,
                             True, want)

    tags = {t.get("Key"): t.get("Value")
            for t in iam.list_policy_tags(PolicyArn=arn).get("Tags", [])}
    if tags.get(PROJECT_TAG_KEY) != inp.project_id:
        raise ForeignResourceError(
            f"IAM policy {name} already exists without the {PROJECT_TAG_KEY}="
            f"{inp.project_id} tag — it was not made by Launchpad for this project; "
            "remove it, then retry"
        )
    default_id = policy["DefaultVersionId"]
    current = _document_of(
        iam.get_policy_version(PolicyArn=arn, VersionId=default_id)["PolicyVersion"]["Document"]
    )
    if digest(current) == want:
        return BoundaryState(name, arn, _version_number(default_id), False, want)

    versions = iam.list_policy_versions(PolicyArn=arn).get("Versions", [])
    stale = sorted((v for v in versions if not v.get("IsDefaultVersion")),
                   key=lambda v: _version_number(v.get("VersionId", "")))
    # make room for the new version: keep the default plus the newest others
    while len(versions) >= MAX_POLICY_VERSIONS and stale:
        oldest = stale.pop(0)
        iam.delete_policy_version(PolicyArn=arn, VersionId=oldest["VersionId"])
        versions = [v for v in versions if v.get("VersionId") != oldest["VersionId"]]
    written = iam.create_policy_version(PolicyArn=arn, PolicyDocument=body,
                                        SetAsDefault=True)["PolicyVersion"]
    version = _version_number(written.get("VersionId", ""))
    log(f"boundary policy {name} updated to v{version}")
    return BoundaryState(name, arn, version, True, want)


def _detail(state: BoundaryState, inp: BoundaryInputs) -> dict[str, Any]:
    return {"version": state.version, "digest": state.digest,
            "legacy": inp.include_legacy, "kb_ids": list(inp.kb_ids),
            "connections": [*inp.oauth2_connections, *inp.api_key_connections]}


class IamBoundaryProvisioner:
    """The `boundary` stage of `project_provision` (registered at import below)."""

    def ensure_boundary(self, ctx: isolation.ProvisionContext) -> str:
        workspace = ctx.workspace
        inp = inputs_for(ctx.project_id, workspace)
        state = ensure_boundary_policy(workspace.client("iam"), inp, ctx.workspace_id,
                                       ctx.log)
        ctx.record(isolation.ProvisionedResource(
            KIND_BOUNDARY_POLICY, resource_id=state.name, arn=state.arn,
            detail=_detail(state, inp),
        ))
        return f"{state.name} v{state.version}" + ("" if state.changed else " (unchanged)")


# ── the deploy path (agent_iam.provision_execution_role) ──────────────────────


def isolated_project(project_id: str | None) -> bool:
    """Whether `project_id` is an active isolated project. A NULL project (a platform
    row) and every shared project — all single-project workspaces — are not."""
    if not project_id:
        return False
    from app.models.projects import Project

    db = SessionLocal()
    try:
        return isolation.is_isolated(db.get(Project, project_id))
    finally:
        db.close()


def _record_from_deploy(project_id: str, workspace_id: str, state: BoundaryState,
                        inp: BoundaryInputs) -> None:
    """Keep the `boundary_policy` row current when a deploy writes a new version. A row
    the provisioning job owns keeps its status; one made here is ready."""
    db = SessionLocal()
    try:
        row = db.get(ProjectResource, (project_id, workspace_id, KIND_BOUNDARY_POLICY))
        if row is None:
            row = ProjectResource(project_id=project_id, workspace_id=workspace_id,
                                  kind=KIND_BOUNDARY_POLICY, status=STATUS_READY)
            db.add(row)
        row.resource_id, row.arn = state.name, state.arn
        row.detail = _detail(state, inp)
        db.commit()
    finally:
        db.close()


def boundary_for_agent(agent: Any, spec: Any, workspace: Any, iam: Any,
                       log: Callable[[str], None] = lambda _m: None) -> BoundaryState | None:
    """The boundary the agent's role must carry, made current for this spec (a new
    Connection or KB widens the explicit lists); None outside isolated projects."""
    project_id = getattr(agent, "project_id", None)
    if not isolated_project(project_id):
        return None
    inp = inputs_for(project_id, workspace, extra_specs=[spec])
    try:
        state = ensure_boundary_policy(iam, inp, workspace.id, log)
    except (isolation.ProvisionError, ForeignResourceError) as exc:
        raise AppError("project.boundary_unavailable", str(exc),
                       {"project_id": project_id}, status_code=409) from exc
    if state.changed:
        _record_from_deploy(project_id, workspace.id, state, inp)
    return state


isolation.set_provisioners(iam=IamBoundaryProvisioner())
