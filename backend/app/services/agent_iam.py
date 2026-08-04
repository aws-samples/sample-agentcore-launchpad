"""Per-agent execution roles derived from the AgentSpec (T3).

Every agent used to assume one shared `launchpad-agent-execution-role` carrying 14
statements, most of them account-wide. The exposure that mattered was not the
wildcards in the abstract — it was that *any* agent could mount *any other agent's*
file systems, read every agent's skill bundles, retrieve from every knowledge base,
and rewrite gateway routing. One prompt-injected agent had every other agent's reach.

So the goal here is **isolation between agents**, with individual actions narrowed
only where that is cheap and provably correct. The discipline matters because an
over-tight policy fails at **invoke** time, not at deploy time: a green
`CreateAgentRuntime` proves nothing about a policy. Several statements below are
deliberately left at `*` with the reason recorded — see `_UNSCOPABLE` notes inline.

Sids are kept identical to the CDK shared role (`infra/stacks/base_stack.py`) so the
two can be diffed statement by statement during review.

The IAM client is injected by the caller: `container.py::_stage_provision` already
takes one for exactly this reason, and it keeps the derivation testable.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.models.ledger import Agent
from app.schemas.agent import AgentSpec

# Inference-profile prefixes: an id like `global.anthropic.claude-sonnet-5` is a
# profile, and invoking it authorizes against the profile ARN *and* the underlying
# foundation-model ARNs. Scoping to only one of the two fails at first invoke.
_PROFILE_PREFIXES = ("global.", "us.", "eu.", "apac.")

_ROLE_PREFIX = "launchpad-agent-"
_ROLE_NAME_MAX = 64  # IAM hard limit
_ID_SUFFIX_LEN = 8

_SANITISE_RE = re.compile(r"[^A-Za-z0-9_-]+")

MANAGED_TAG_KEY = "launchpad:agent-id"

BUILTIN_CODE_INTERPRETER = "code-interpreter"
BUILTIN_BROWSER = "browser"


@dataclass(frozen=True)
class RoleContext:
    """Account-level facts the policy needs. Passed in rather than read from
    settings so `policy_document` stays a pure function."""

    account_id: str
    region: str
    artifacts_bucket: str
    ecr_repo_arn: str
    memory_id: str = ""


def context_from_settings(settings: Any) -> RoleContext:
    """Build a `RoleContext` from the resolved settings."""
    resources = settings.resources or {}
    repo = resources.get("ecr_repo", "launchpad-agents")
    return RoleContext(
        account_id=settings.account_id,
        region=settings.region,
        artifacts_bucket=resources.get("artifacts_bucket", ""),
        ecr_repo_arn=(
            f"arn:aws:ecr:{settings.region}:{settings.account_id}:repository/{repo}"
        ),
        memory_id=resources.get("memory_id", ""),
    )


def live_runtime_role_arn(runtime_detail: dict[str, Any] | None, settings: Any) -> str:
    """The role the running runtime is already using, else the shared role.

    Used by the paths that mint a candidate *version of an existing runtime* (canary,
    A/B): a candidate stands in for production, so it must carry production's role
    rather than a wider shared one — otherwise the candidate is measured with
    permissions production does not have, and a promotion inherits them.

    Read from the live resource rather than derived from the agent name, because
    deriving it would guess wrong for any agent deployed before per-agent roles
    existed: the name would resolve to a role that does not exist and
    UpdateAgentRuntime would fail. The live value needs no migration state.
    """
    live = (runtime_detail or {}).get("roleArn") or ""
    return live or shared_role_arn(settings)


def shared_role_arn(settings: Any) -> str:
    """The pre-existing shared role, still used by candidate versions and as the
    fallback when per-agent roles are turned off."""
    return (settings.resources or {}).get("execution_role_arn", "")


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------

def role_name_for(agent_name: str, agent_id: str) -> str:
    """`launchpad-agent-{name}-{id8}`, inside IAM's 64-character limit.

    The id suffix is not decoration: agent names are user-supplied, so truncating
    the name alone would collide two agents whose names share a prefix. Keeping the
    id *in the name* (rather than only in a tag) also lets an operator reading the
    IAM console map a role back to a ledger row.
    """
    suffix = (agent_id or "")[:_ID_SUFFIX_LEN] or "00000000"
    room = _ROLE_NAME_MAX - len(_ROLE_PREFIX) - 1 - len(suffix)
    stem = _SANITISE_RE.sub("-", agent_name or "agent").strip("-")[:room] or "agent"
    return f"{_ROLE_PREFIX}{stem}-{suffix}"


def fs_policy_name(agent_name: str) -> str:
    """Name of the BYO-mount inline policy. Unchanged from the shared-role era so a
    migration can find and remove the old one."""
    return f"launchpad-fs-{agent_name}"


def capability_policy_name(agent_name: str) -> str:
    return f"launchpad-caps-{agent_name}"


# ---------------------------------------------------------------------------
# trust
# ---------------------------------------------------------------------------

def trust_policy(ctx: RoleContext, runtime_arn: str | None = None) -> dict:
    """AgentCore's assume-role policy.

    `aws:SourceArn` can only be added once the runtime exists, and the role must
    exist *before* CreateAgentRuntime — so the first create is account-scoped and a
    later reconcile can tighten it. Whether AgentCore actually sends SourceArn is
    unverified against a live account, so callers gate that on a setting: getting it
    wrong locks the agent out on its *second* deploy.
    """
    conditions: dict[str, Any] = {"StringEquals": {"aws:SourceAccount": ctx.account_id}}
    if runtime_arn:
        conditions["ArnEquals"] = {"aws:SourceArn": runtime_arn}
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": conditions,
        }],
    }


# ---------------------------------------------------------------------------
# capability derivation
# ---------------------------------------------------------------------------

def model_resources(model_id: str, ctx: RoleContext) -> list[str]:
    """Resource ARNs authorizing one model id.

    A profile id authorizes against both the profile and the foundation models it
    fronts. An id matching neither shape falls back to the foundation-model wildcard
    **on purpose**: custom ids are a first-class feature here (see the comment on
    `AgentSpec.model_id` — the valid id space cannot be enumerated from the account),
    so narrowing a shape we do not recognise would break the agent rather than
    protect it.
    """
    if not model_id:
        return ["*"]
    profile_prefix = next(
        (p for p in _PROFILE_PREFIXES if model_id.startswith(p)), None
    )
    if profile_prefix:
        bare = model_id[len(profile_prefix):]
        return [
            f"arn:aws:bedrock:*::foundation-model/{bare}",
            f"arn:aws:bedrock:{ctx.region}:{ctx.account_id}:inference-profile/{model_id}",
        ]
    if re.match(r"^[a-z0-9-]+\.[A-Za-z0-9.:-]+$", model_id):
        return [f"arn:aws:bedrock:*::foundation-model/{model_id}"]
    return ["arn:aws:bedrock:*::foundation-model/*"]


def _uses_gateway(spec: AgentSpec) -> bool:
    """Whether anything in the spec needs an AgentCore workload token."""
    if spec.knowledge_bases:
        return True  # harness KBs ride the shared KB gateway
    return any(tool.type in ("gateway", "mcp") for tool in spec.tools)


def _builtin_names(spec: AgentSpec) -> set[str]:
    return {tool.name for tool in spec.tools if tool.type == "builtin"}


def policy_document(spec: AgentSpec, ctx: RoleContext) -> dict:
    """The capability policy for one agent.

    Statements appear only when the spec calls for them. Sids match the shared CDK
    role so the two can be diffed.
    """
    statements: list[dict[str, Any]] = []

    # ---- models: always needed, scoped to the configured id ----
    statements.append({
        "Sid": "BedrockModels",
        "Effect": "Allow",
        "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
        "Resource": model_resources(spec.model_id, ctx),
    })

    if spec.model_source == "mantle":
        # Bedrock Mantle is a SEPARATE IAM service from bedrock — bedrock:InvokeModel
        # does not cover it. Without these a Mantle agent reaches ACTIVE and then
        # fails its first invoke with 401 bedrock-mantle:CreateInference.
        statements.append({
            "Sid": "BedrockMantleInference",
            "Effect": "Allow",
            "Action": [
                "bedrock-mantle:Get*",
                "bedrock-mantle:List*",
                "bedrock-mantle:CreateInference",
            ],
            # Mantle models are hosted outside the stack region, so the region
            # segment stays wildcarded, and projects are not per-agent.
            "Resource": [f"arn:aws:bedrock-mantle:*:{ctx.account_id}:project/*"],
        })
        statements.append({
            # UNSCOPABLE: minting the short-lived bearer token has no resource.
            "Sid": "BedrockMantleCallWithBearerToken",
            "Effect": "Allow",
            "Action": ["bedrock-mantle:CallWithBearerToken"],
            "Resource": "*",
        })
        statements.append({
            # Third-party Mantle families are fronted by Marketplace subscriptions.
            # The CalledViaLast condition is what makes the wildcard acceptable: the
            # role cannot subscribe to anything on its own initiative.
            "Sid": "MarketplaceOperationsFromBedrockMantleFor3pModels",
            "Effect": "Allow",
            "Action": ["aws-marketplace:Subscribe", "aws-marketplace:ViewSubscriptions"],
            "Resource": "*",
            "Condition": {
                "StringEquals": {"aws:CalledViaLast": "bedrock-mantle.amazonaws.com"}
            },
        })

    # ---- memory ----
    # NOTE: the memory is a shared singleton, so this scopes to that one resource
    # but does NOT give per-agent memory isolation. Partitioning is done by folding
    # the agent id into the actor id (see services/memory.py::scoped_actor), not by
    # IAM. Per-agent memories would be a different change.
    if spec.memory.short_term or spec.memory.long_term:
        memory_resource = (
            f"arn:aws:bedrock-agentcore:{ctx.region}:{ctx.account_id}:memory/{ctx.memory_id}"
            if ctx.memory_id else "*"
        )
        statements.append({
            "Sid": "AgentCoreMemory",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:CreateEvent",
                "bedrock-agentcore:GetEvent",
                "bedrock-agentcore:ListEvents",
                "bedrock-agentcore:ListSessions",
                "bedrock-agentcore:ListActors",
                "bedrock-agentcore:RetrieveMemoryRecords",
                "bedrock-agentcore:GetMemoryRecord",
                "bedrock-agentcore:ListMemoryRecords",
            ],
            "Resource": memory_resource,
        })

    # ---- identity / workload tokens ----
    if _uses_gateway(spec):
        statements.append({
            # UNSCOPABLE: workload-token actions take no resource.
            "Sid": "AgentCoreWorkloadIdentity",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:GetResourceApiKey",
                "bedrock-agentcore:GetResourceOauth2Token",
                "bedrock-agentcore:GetWorkloadAccessToken",
                "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
            ],
            "Resource": "*",
        })
        statements.append({
            "Sid": "IdentityVaultSecrets",
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [
                f"arn:aws:secretsmanager:{ctx.region}:{ctx.account_id}"
                ":secret:bedrock-agentcore-identity!*"
            ],
        })

    # ---- builtin tools ----
    builtins = _builtin_names(spec)
    if BUILTIN_CODE_INTERPRETER in builtins:
        statements.append({
            "Sid": "AgentCoreCodeInterpreter",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:InvokeCodeInterpreter",
                "bedrock-agentcore:StartCodeInterpreterSession",
                "bedrock-agentcore:StopCodeInterpreterSession",
                "bedrock-agentcore:GetCodeInterpreterSession",
            ],
            "Resource": "*",
        })
    if BUILTIN_BROWSER in builtins:
        statements.append({
            "Sid": "AgentCoreBrowser",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:ConnectBrowserAutomationStream",
                "bedrock-agentcore:ConnectBrowserLiveViewStream",
                "bedrock-agentcore:StartBrowserSession",
                "bedrock-agentcore:StopBrowserSession",
                "bedrock-agentcore:GetBrowserSession",
            ],
            "Resource": "*",
        })

    # ---- container image pull ----
    if spec.method == "container":
        statements.append({
            "Sid": "EcrPull",
            "Effect": "Allow",
            "Action": ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"],
            "Resource": [ctx.ecr_repo_arn],
        })
        statements.append({
            # UNSCOPABLE: ecr:GetAuthorizationToken takes no resource by design.
            "Sid": "EcrAuth",
            "Effect": "Allow",
            "Action": ["ecr:GetAuthorizationToken"],
            "Resource": "*",
        })

    # ---- attached skill bundles: scoped to this agent's skills ----
    if spec.skills and ctx.artifacts_bucket:
        prefixes = sorted({_skill_prefix(path) for path in spec.skills})
        statements.append({
            "Sid": "SkillBundleObjects",
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": [
                f"arn:aws:s3:::{ctx.artifacts_bucket}/{prefix}*" for prefix in prefixes
            ],
        })
        statements.append({
            "Sid": "SkillBundleList",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": [f"arn:aws:s3:::{ctx.artifacts_bucket}"],
            "Condition": {"StringLike": {"s3:prefix": [f"{p}*" for p in prefixes]}},
        })

    # ---- managed knowledge bases: scoped to the attached ones ----
    if spec.knowledge_bases:
        statements.append({
            "Sid": "ManagedKbRetrieval",
            "Effect": "Allow",
            "Action": ["bedrock:Retrieve", "bedrock:GetKnowledgeBase"],
            "Resource": [
                f"arn:aws:bedrock:{ctx.region}:{ctx.account_id}:knowledge-base/{kb.kb_id}"
                for kb in spec.knowledge_bases
            ],
        })
        statements.append({
            # UNSCOPABLE: AgenticRetrieveStream does not support resource scoping, so
            # any Launchpad runtime with KBs attached can agentic-retrieve against
            # any KB in the account. Accepted and unchanged from the shared role —
            # launchpad-gateway-role carries the same grant for the harness channel.
            "Sid": "ManagedKbAgenticRetrieval",
            "Effect": "Allow",
            "Action": ["bedrock:AgenticRetrieveStream"],
            "Resource": "*",
        })

    # ---- A2A agents legitimately invoke other runtimes ----
    if spec.protocol == "a2a":
        statements.append({
            "Sid": "A2AInvokePeerRuntimes",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
            "Resource": [
                f"arn:aws:bedrock-agentcore:{ctx.region}:{ctx.account_id}:runtime/*"
            ],
        })

    # ---- telemetry ----
    # Writes only. The shared role also granted StartQuery / GetQueryResults /
    # FilterLogEvents / GetLogEvents / DescribeLogGroups — those are the console's
    # read paths and have no business on a workload role.
    statements.append({
        "Sid": "Telemetry",
        "Effect": "Allow",
        "Action": [
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents",
            "logs:DescribeLogStreams",
        ],
        "Resource": [
            f"arn:aws:logs:{ctx.region}:{ctx.account_id}:log-group:"
            "/aws/bedrock-agentcore/runtimes/*",
            f"arn:aws:logs:{ctx.region}:{ctx.account_id}:log-group:"
            "/aws/bedrock-agentcore/runtimes/*:log-stream:*",
        ],
    })
    statements.append({
        # UNSCOPABLE: X-Ray segment ingestion and PutMetricData take no resource.
        "Sid": "TelemetryTracing",
        "Effect": "Allow",
        "Action": [
            "xray:PutTraceSegments",
            "xray:PutTelemetryRecords",
            "xray:GetSamplingRules",
            "xray:GetSamplingTargets",
            "cloudwatch:PutMetricData",
        ],
        "Resource": "*",
    })

    return {"Version": "2012-10-17", "Statement": statements}


def _skill_prefix(skill_path: str) -> str:
    """`s3://bucket/skills/name/` or `skills/name/` → `skills/name/`."""
    path = skill_path
    if path.startswith("s3://"):
        path = path.split("/", 3)[3] if path.count("/") >= 3 else ""
    path = path.lstrip("/")
    return path if path.endswith("/") else f"{path}/"


# ---------------------------------------------------------------------------
# BYO file-system mounts
#
# Moved here verbatim from deployer/container.py so the mount grant lands on the
# agent's own role instead of accumulating on a principal every agent assumes.
# DO NOT re-derive these statement shapes: the AWS devguide's example policy is
# wrong and incomplete, and the correct shape below was established by IAM
# simulator plus live UpdateAgentRuntime probes (2026-07-13).
# ---------------------------------------------------------------------------

def fs_policy_document(spec: AgentSpec) -> dict | None:
    """Inline policy granting mount access to the BYO access points.

    S3 Files APs embed the file-system ARN (strip '/access-point/…'); EFS APs don't —
    Resource '*' scoped by the AccessPointArn condition.

    The devguide's example (one combined conditioned statement with
    ClientMount/ClientWrite/GetAccessPoint) is WRONG and incomplete:
    - GetAccessPoint authorizes on the access-point ARN and does not carry the
      s3files:AccessPointArn condition key → its own unconditioned statement;
    - AgentCore's create/update validation ALSO requires ListMountTargets on the
      file system (undocumented).
    """
    statements: list[dict] = []
    if spec.filesystem.s3_files:
        arns = [m.access_point_arn for m in spec.filesystem.s3_files]
        fs_arns = sorted({a.split("/access-point/")[0] for a in arns})
        statements.append({
            "Effect": "Allow",
            "Action": ["s3files:ClientMount", "s3files:ClientWrite"],
            "Resource": fs_arns,
            "Condition": {"ArnEquals": {"s3files:AccessPointArn": arns}},
        })
        statements.append({
            "Effect": "Allow",
            "Action": ["s3files:GetAccessPoint"],
            "Resource": arns,
        })
        statements.append({
            "Effect": "Allow",
            "Action": ["s3files:ListMountTargets"],
            "Resource": fs_arns,
        })
    if spec.filesystem.efs:
        arns = [m.access_point_arn for m in spec.filesystem.efs]
        statements.append({
            "Effect": "Allow",
            "Action": ["elasticfilesystem:ClientMount", "elasticfilesystem:ClientWrite"],
            "Resource": "*",
            "Condition": {"ArnEquals": {"elasticfilesystem:AccessPointArn": arns}},
        })
    if not statements:
        return None
    return {"Version": "2012-10-17", "Statement": statements}


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def ensure_role(
    iam: Any,
    agent: Agent,
    spec: AgentSpec,
    ctx: RoleContext,
    log: Callable[[str], None] = lambda _m: None,
    runtime_arn: str | None = None,
) -> str:
    """Create or reconcile the agent's role; return its ARN.

    Idempotent, because `resume_pending_jobs()` re-enters the provision stage. An
    existing role of the same name is **adopted** rather than treated as an error: a
    previous delete may have half-failed, and failing here would wedge re-creating an
    agent under a name that was used before.
    """
    name = role_name_for(agent.name, agent.id)
    trust = json.dumps(trust_policy(ctx, runtime_arn))
    try:
        created = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=trust,
            Description=f"Launchpad execution role for agent {agent.name} ({agent.id})",
            Tags=[
                {"Key": MANAGED_TAG_KEY, "Value": agent.id},
                {"Key": "launchpad:managed", "Value": "true"},
            ],
        )
        role_arn = created["Role"]["Arn"]
        log(f"created execution role {name}")
    except Exception as exc:  # noqa: BLE001 — the SDK's typed error is client-specific
        if not _is_already_exists(exc):
            raise
        role_arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        # Adopting: refresh the trust policy so a tightened condition still lands.
        iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust)
        log(f"reusing existing execution role {name}")

    iam.put_role_policy(
        RoleName=name,
        PolicyName=capability_policy_name(agent.name),
        PolicyDocument=json.dumps(policy_document(spec, ctx)),
    )
    _sync_fs_policy(iam, name, agent, spec, log)
    return role_arn


def _sync_fs_policy(
    iam: Any, role_name: str, agent: Agent, spec: AgentSpec, log: Callable[[str], None]
) -> None:
    """Attach the BYO-mount policy, or drop a stale one when the mounts were removed
    on re-publish."""
    policy = fs_policy_document(spec)
    name = fs_policy_name(agent.name)
    if policy:
        iam.put_role_policy(
            RoleName=role_name, PolicyName=name, PolicyDocument=json.dumps(policy)
        )
        log(f"inline policy {name} attached (BYO file-system mounts)")
        return
    try:
        iam.delete_role_policy(RoleName=role_name, PolicyName=name)
        log(f"inline policy {name} removed (no BYO mounts)")
    except Exception:  # noqa: BLE001 — absent on most agents, nothing to clean
        pass


def provision_execution_role(
    agent: Agent,
    spec: AgentSpec,
    settings: Any,
    log: Callable[[str], None] = lambda _m: None,
    iam: Any = None,
) -> tuple[str, str]:
    """The provision-stage entry point shared by all three deployers.

    Returns `(role_arn, detail)`. Falls back to the shared role when per-agent roles
    are switched off, so the two paths differ in one place rather than three.
    """
    if not settings.per_agent_execution_roles:
        arn = shared_role_arn(settings)
        if not arn:
            raise RuntimeError(
                "execution_role_arn missing from config/launchpad.yaml — run "
                "scripts/bootstrap.py"
            )
        log(f"per-agent roles disabled — reusing shared execution role {arn}")
        return arn, "iam role reused · launchpad-base (shared)"

    if iam is None:
        import boto3

        iam = boto3.client("iam", region_name=settings.region)
    ctx = context_from_settings(settings)
    arn = ensure_role(iam, agent, spec, ctx, log)
    name = role_name_for(agent.name, agent.id)
    detail = f"iam role · {name}"
    if fs_policy_document(spec):
        detail += f" (+ {fs_policy_name(agent.name)})"
    return arn, detail


def delete_execution_role(
    agent: Agent, settings: Any, log: Callable[[str], None] = lambda _m: None,
    iam: Any = None,
) -> bool:
    """Delete the agent's role, if it has one. Never raises."""
    if not settings.per_agent_execution_roles:
        return True  # the shared role is not ours to delete
    if iam is None:
        import boto3

        iam = boto3.client("iam", region_name=settings.region)
    return delete_role(iam, agent, log)


def delete_role(
    iam: Any, agent: Agent, log: Callable[[str], None] = lambda _m: None
) -> bool:
    """Delete the agent's role and its inline policies. Never raises.

    A failed delete must not block deleting the agent, but it must leave the role
    **findable** — hence the log line naming it, and the `launchpad:managed` tag that
    lets `scripts/teardown.py` sweep orphans.
    """
    name = role_name_for(agent.name, agent.id)
    try:
        listed = iam.list_role_policies(RoleName=name).get("PolicyNames", [])
    except Exception as exc:  # noqa: BLE001
        if _is_no_such_entity(exc):
            return True  # already gone
        log(f"could not list policies on {name}: {exc}")
        listed = []
    for policy_name in listed:
        try:
            iam.delete_role_policy(RoleName=name, PolicyName=policy_name)
        except Exception as exc:  # noqa: BLE001
            log(f"could not delete inline policy {policy_name} on {name}: {exc}")
    try:
        iam.delete_role(RoleName=name)
        log(f"deleted execution role {name}")
        return True
    except Exception as exc:  # noqa: BLE001
        if _is_no_such_entity(exc):
            return True
        log(
            f"could not delete execution role {name}: {exc} — it is tagged "
            f"{MANAGED_TAG_KEY}={agent.id} and can be swept by scripts/teardown.py"
        )
        return False


def _is_already_exists(exc: Exception) -> bool:
    return "EntityAlreadyExists" in f"{type(exc).__name__}{exc}"


def _is_no_such_entity(exc: Exception) -> bool:
    return "NoSuchEntity" in f"{type(exc).__name__}{exc}"


# ---------------------------------------------------------------------------
# IAM eventual consistency
# ---------------------------------------------------------------------------

# Create/UpdateAgentRuntime validates the execution role server-side, and a role or
# policy written moments earlier can still be invisible inside AWS's IAM propagation
# window. Originally observed as "missing required permissions" after rewriting an
# inline policy (live hit 2026-07-13 on an access-point ARN change); a **brand-new
# role** is a longer window and can surface as an assume-role or AccessDenied
# wording instead, so the predicate covers all of them.
_PROPAGATION_MARKERS = (
    "missing required permissions",
    "is not authorized to perform: sts:assumerole",
    "unable to assume",
    "cannot be assumed",
    "accessdenied",
    "access denied",
)


def is_iam_propagation_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _PROPAGATION_MARKERS)


def retry_iam_propagation(
    fn: Callable[[], Any],
    log: Callable[[str], None],
    attempts: int = 6,
    delay_s: int = 10,
    sleeper: Callable[[float], None] = time.sleep,
) -> Any:
    """Retry `fn` only while the failure looks like IAM propagation."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if not is_iam_propagation_error(exc) or attempt == attempts - 1:
                raise
            log(
                "execution-role permissions not yet visible (IAM propagation) — "
                f"retry {attempt + 1}/{attempts - 1} in {delay_s}s"
            )
            sleeper(delay_s)
