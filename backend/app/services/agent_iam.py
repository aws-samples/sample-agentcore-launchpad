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

Phase 2 isolation (projects design §10.3, R10-iam): the role of an agent in an
*isolated* project additionally carries the project's permissions boundary
(`services/project_iam.py`) and the tags `launchpad:project` / `launchpad:workspace` /
`launchpad:isolation=isolated`, and such an agent never falls back to the shared
execution role. Agents of shared projects — every single-project workspace — are
untouched.
"""

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from app.core.config import get_settings
from app.core.errors import AppError, cleanup_error, is_not_found
from app.core.regions import partition_for_region
from app.models.ledger import Agent
from app.schemas.agent import (
    INFERENCE_PROFILE_PREFIXES,
    AgentSpec,
    byoc_model_target,
)
from app.schemas.strands_builder import uses_code_interpreter
from app.services.workspace import WorkspaceContext
from app.templates import context_layer_support

logger = logging.getLogger(__name__)

# Inference-profile prefixes: an id like `global.anthropic.claude-sonnet-5` is a
# profile, and invoking it authorizes against the profile ARN *and* the underlying
# foundation-model ARNs. Scoping to only one of the two fails at first invoke.
_PROFILE_PREFIXES = INFERENCE_PROFILE_PREFIXES

_ROLE_PREFIX = "launchpad-agent-"
_ROLE_NAME_MAX = 64  # IAM hard limit
_ID_SUFFIX_LEN = 8

_SANITISE_RE = re.compile(r"[^A-Za-z0-9_-]+")

MANAGED_TAG_KEY = "launchpad:agent-id"

# Other modules add the statements their features need to every per-agent policy
# without editing this file: each builder is called as `build(spec, resources)` with
# the agent's spec and its `RoleContext`, and returns statements to append (or none).
# A builder whose grant must also hold inside isolated projects registers the matching
# ceiling in `project_iam.EXTRA_BOUNDARY_STATEMENT_BUILDERS`.
StatementBuilder = Callable[[AgentSpec, "RoleContext"], list[dict[str, Any]] | None]
EXTRA_STATEMENT_BUILDERS: list[StatementBuilder] = []

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
    # The OAuth2 credential provider the KB gateway's CLIENT_CREDENTIALS outbound
    # auth uses (bootstrap writes it as `oauth_provider_arn`). Only the
    # system-preset KB path scopes to it; ordinary agents keep the family grant.
    oauth_provider_arn: str = ""
    # an isolated project's repository holds the agent's new images; the shared one
    # may still hold its earlier builds (projects design §10.10)
    extra_ecr_repo_arns: tuple[str, ...] = ()
    # the project's application inference profiles standing in for the spec's model
    # (§10.8, `deployer/model_profile.py`): granted next to the model's own resources
    inference_profile_arns: tuple[str, ...] = ()

    @property
    def partition(self) -> str:
        return partition_for_region(self.region)


def role_context(workspace: WorkspaceContext, project_id: str | None = None) -> RoleContext:
    """Build a `RoleContext` from the workspace the agent is deployed into. With the
    agent's `project_id`, an isolated project's ECR repository comes first (a shared
    project — every single-project workspace — reads exactly as without it). The
    memory and the gateway credential provider are the project's own once its
    isolation is provisioned (`project_memory.memory_resources`,
    `project_gateway.effective_resources`; default: the current project)."""
    # imported here: project_memory imports project_isolation → project_iam → this
    from app.core.project_scope import current_project
    from app.services import project_gateway, project_memory

    pid = project_id if project_id is not None else current_project()
    resources = project_memory.memory_resources(
        project_gateway.effective_resources(dict(workspace.resources or {}), pid, workspace.id),
        workspace.id, pid)
    base = f"arn:{workspace.partition}:ecr:{workspace.region}:{workspace.account_id}:repository"
    if project_id:
        from app.services import project_storage

        repos = project_storage.ecr_repos(workspace, project_id)
    else:
        repos = [resources.get("ecr_repo", "launchpad-agents")]
    return RoleContext(
        account_id=workspace.account_id,
        region=workspace.region,
        artifacts_bucket=resources.get("artifacts_bucket", ""),
        ecr_repo_arn=f"{base}/{repos[0]}",
        memory_id=resources.get("memory_id", ""),
        oauth_provider_arn=resources.get("oauth_provider_arn", ""),
        extra_ecr_repo_arns=tuple(f"{base}/{repo}" for repo in repos[1:]),
    )


def oauth_provider_name(provider_arn: str) -> str:
    """`…:token-vault/default/oauth2credentialprovider/<name>` → `<name>`."""
    return provider_arn.rsplit("/", 1)[-1] if provider_arn else ""


def live_runtime_role_arn(
    runtime_detail: dict[str, Any] | None,
    workspace: WorkspaceContext,
    project_id: str | None = None,
) -> str:
    """The role the running runtime is already using, else the shared role.

    Used by the paths that mint a candidate *version of an existing runtime* (canary,
    A/B): a candidate stands in for production, so it must carry production's role
    rather than a wider shared one — otherwise the candidate is measured with
    permissions production does not have, and a promotion inherits them.

    Read from the live resource rather than derived from the agent name, because
    deriving it would guess wrong for any agent deployed before per-agent roles
    existed: the name would resolve to a role that does not exist and
    UpdateAgentRuntime would fail. The live value needs no migration state.

    A candidate of an agent in an isolated project (`project_id`) never falls back to
    the shared role (§10.3): 409 `project.isolation_requires_per_agent_role`.
    """
    live = (runtime_detail or {}).get("roleArn") or ""
    if not live:
        _refuse_shared_role_if_isolated(project_id)
    return live or shared_role_arn(workspace)


def shared_role_arn(workspace: WorkspaceContext) -> str:
    """The pre-existing shared role, still used by candidate versions and as the
    fallback when per-agent roles are turned off."""
    return (workspace.resources or {}).get("execution_role_arn", "")


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
            f"arn:{ctx.partition}:bedrock:*::foundation-model/{bare}",
            f"arn:{ctx.partition}:bedrock:{ctx.region}:{ctx.account_id}:inference-profile/{model_id}",
        ]
    if re.match(r"^[a-z0-9-]+\.[A-Za-z0-9.:-]+$", model_id):
        return [f"arn:{ctx.partition}:bedrock:*::foundation-model/{model_id}"]
    return [f"arn:{ctx.partition}:bedrock:*::foundation-model/*"]


# Strands Studio canvas flows carry their models on the nodes, not in
# ``spec.model_id``: the publish sends none, so that field is always AgentSpec's
# default and scoping the role to it silently refused every other model at
# invoke. Provider strings and the node types that carry a model mirror
# ``frontend/src/studio`` (``MANTLE_PROVIDER`` in lib/models.ts, the agent-node
# filter in lib/graph-code-generator.ts); a node without a model id generates
# the canvas fallback, ``studio_fallback_model_id()``.
STUDIO_MODEL_NODE_TYPES = frozenset({"agent", "orchestrator-agent", "swarm"})
# The canvas's own fallback (``DEFAULT_MODEL_ID`` in frontend/src/studio/lib/
# models.ts), which deliberately does not follow the platform default: changing
# it would silently change every existing flow's model-less nodes. Configurable as
# settings.studio_fallback_model_id (env LAUNCHPAD_STUDIO_FALLBACK_MODEL_ID) for
# accounts without Claude; the console reads the same value from /api/models/defaults.


def studio_fallback_model_id() -> str:
    return get_settings().studio_fallback_model_id.strip()


STUDIO_BEDROCK_PROVIDER = "AWS Bedrock"
STUDIO_MANTLE_PROVIDER = "Amazon Bedrock (Mantle)"


def studio_node_models(spec: AgentSpec) -> list[tuple[str, str]]:
    """``(provider, model id)`` for every model-bearing node of a studio flow."""
    if spec.method != "studio" or not isinstance(spec.studio_flow, dict):
        return []
    models: list[tuple[str, str]] = []
    for node in spec.studio_flow.get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") not in STUDIO_MODEL_NODE_TYPES:
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        provider = data.get("modelProvider") or STUDIO_BEDROCK_PROVIDER
        model = data.get("modelId") or (
            studio_fallback_model_id() if provider == STUDIO_BEDROCK_PROVIDER else ""
        )
        models.append((str(provider), str(model)))
    return models


def uses_mantle(spec: AgentSpec) -> bool:
    """Whether the agent calls Bedrock Mantle (its own IAM service)."""
    return spec.model_source == "mantle" or any(
        provider == STUDIO_MANTLE_PROVIDER for provider, _ in studio_node_models(spec)
    )


def allowed_model_resources(spec: AgentSpec, ctx: RoleContext) -> list[str]:
    """Union of `model_resources` over every model the spec permits, deduped in
    order. One entry for every method except byoc, whose ``allowed_models`` list
    may authorize several — each still scoped to its exact id, never widened —
    and studio, which authorizes each native-Bedrock model its flow's nodes use. A
    summarizing conversation manager's ``summary_model`` is authorized beside the
    agent's own model."""
    if spec.method == "studio":
        ids = [m for provider, m in studio_node_models(spec) if provider == STUDIO_BEDROCK_PROVIDER]
        resources: list[str] = []
        # a flow with no Bedrock node (Mantle / OpenAI only) keeps the spec's id so
        # the statement stays well-formed
        for model_id in ids or [spec.model_id]:
            for resource in model_resources(model_id, ctx):
                if resource not in resources:
                    resources.append(resource)
        return resources
    if spec.method != "byoc":
        resources = model_resources(spec.model_id, ctx)
        # the summarizing conversation manager calls its own (Bedrock) model (SB-1)
        summary = spec.conversation.summary_model if spec.conversation else None
        if summary:
            resources = list(dict.fromkeys([*resources, *model_resources(summary, ctx)]))
        return resources
    resources: list[str] = []
    for selection in spec.allowed_model_ids:
        kind, model_id = byoc_model_target(selection)
        if selection.startswith("arn:"):
            partition = selection.split(":", 2)[1]
            resource = selection
        else:
            partition = "aws"
            scope = f"{ctx.region}:{ctx.account_id}" if kind == "inference-profile" else "*:"
            resource = f"arn:{partition}:bedrock:{scope}:{kind}/{model_id}"
        if kind == "inference-profile":
            prefix = next(p for p in _PROFILE_PREFIXES if model_id.startswith(p))
            resources.append(
                f"arn:{partition}:bedrock:*::foundation-model/{model_id[len(prefix):]}"
            )
        resources.append(resource)
    return list(dict.fromkeys(resources))


def _uses_gateway(spec: AgentSpec) -> bool:
    """Whether anything in the spec needs the FAMILY-WIDE workload-token grant.

    A remote MCP tool whose config declares ``auth: "none"`` is a public,
    unauthenticated server (e.g. the AWS Knowledge MCP): no token, no vault secret,
    so no identity grant. A tool carrying its own ``ToolRef.auth`` block is
    excluded too — it gets the narrow per-Connection statements from
    ``_tool_auth_statements`` instead of widening this one. Every other MCP ref
    keeps the historical grant — an existing agent's outbound auth must not
    change under it.
    """
    if spec.knowledge_bases:
        return True  # harness KBs ride the shared KB gateway
    if spec.method == "harness" and context_layer_support.via_gateway(spec):
        # the managed COA gateway is deployed as an M2M gateway attachment, as a
        # Registry-attached user-trusting gateway is (each console turn swaps in the
        # member's JWT, but the deployed configuration stays one the role can run).
        # A Strands runtime in gateway mode needs no grant: it calls the gateway over
        # plain HTTPS with the member's JWT from the payload, which the gateway's
        # CUSTOM_JWT authorizer checks (no IAM call, no token exchange).
        return True
    for tool in spec.tools:
        if tool.type == "gateway":
            return True
        if (
            tool.type == "mcp"
            and tool.auth is None
            and (tool.config or {}).get("auth") != "none"
        ):
            return True
    return False


def _builtin_names(spec: AgentSpec) -> set[str]:
    return {tool.name for tool in spec.tools if tool.type == "builtin"}


# AgentCore gateway ids: `<name>-<10 lowercase alphanumerics>`. Anything else in a
# member-supplied ToolRef (a `*`, an ARN fragment) must never reach a Resource.
_GATEWAY_ID_RE = re.compile(r"[0-9a-z](?:[0-9a-z-]{0,98})-[0-9a-z]{10}")


def _gateway_ids(spec: AgentSpec) -> list[str]:
    """Gateway ids named by the spec's Registry-resolved gateway ToolRefs."""
    ids = {
        str((tool.config or {}).get("gateway_id"))
        for tool in spec.tools
        if tool.type == "gateway" and (tool.config or {}).get("gateway_id")
    }
    return sorted(gateway_id for gateway_id in ids if _GATEWAY_ID_RE.fullmatch(gateway_id))


def runtime_name_base(agent_name: str) -> str:
    """The deterministic part of an agent's Runtime name.

    ``deployer.zip_runtime.sanitize_runtime_name`` appends ``_<6 hex>`` to this
    for uniqueness; it lives here because the workload-identity scope below
    must agree with it exactly. The agent name is immutable after create (the
    re-publish route refuses a rename), so the base never drifts from the
    runtime that already exists.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", agent_name).strip("_")[:40] or "agent"


def own_workload_identity_arn(spec: AgentSpec, ctx: RoleContext) -> str:
    """The agent's OWN auto-created workload identity, as an IAM resource.

    The Runtime names that identity after the runtime id — ``<runtimeName>-
    <10-char suffix>`` (deployer/return_url.py, live-verified 2026-09-19;
    evidence docs/identity-e2e-evidence-p2.md / -p3.md) — and the runtime name
    is ``<base>_<6 hex>``. Neither random part is known at provision time,
    so the single-character ``?`` wildcard pins the 6-hex segment exactly: the
    pattern matches this agent's runtimes, not another agent whose name merely
    starts with the same characters (``a`` vs ``a_b``).
    """
    base = f"arn:aws:bedrock-agentcore:{ctx.region}:{ctx.account_id}"
    return (
        f"{base}:workload-identity-directory/default/workload-identity/"
        f"{runtime_name_base(spec.name)}_??????-*"
    )


def _tool_auth_statements(spec: AgentSpec, ctx: RoleContext) -> list[dict[str, Any]]:
    """Per-Connection grants for tools carrying ``ToolRef.auth``.

    Follows the devguide-exact shape of ``_preset_kb_oauth_statements``: the
    token action on the token vault + the workload-identity directory + the exact
    provider ARNs, and ``GetSecretValue`` on the vault's fixed secret prefix for
    exactly those Connection names — never the family-wide
    ``bedrock-agentcore-identity!*``. An agent with no ``auth`` tools gets nothing
    from here (grant only when used).
    """
    wanted = {(tool.auth.connection, tool.auth.kind) for tool in spec.tools if tool.auth}
    # SB-2: a direct model provider reads its key through the same token-vault
    # path as an ``api_key`` tool, so its Connection is granted the same way.
    if spec.model_provider is not None:
        wanted.add((spec.model_provider.connection, "api_key"))
    if not wanted:
        return []
    base = f"arn:aws:bedrock-agentcore:{ctx.region}:{ctx.account_id}"
    secret_base = (
        f"arn:aws:secretsmanager:{ctx.region}:{ctx.account_id}:secret:"
        "bedrock-agentcore-identity!default"
    )
    oauth2 = sorted({name for name, kind in wanted if kind == "oauth2"})
    api_key = sorted({name for name, kind in wanted if kind == "api_key"})
    # The workload identity is auto-created by the Runtime and named after the
    # runtime id, unknown at provision time — scoped to this agent's own name
    # pattern (``own_workload_identity_arn``), never the whole directory.
    identity_resources = [
        f"{base}:token-vault/default",
        f"{base}:workload-identity-directory/default",
        own_workload_identity_arn(spec, ctx),
    ]
    statements: list[dict[str, Any]] = []
    if oauth2:
        statements.append({
            "Sid": "ToolAuthOauth2Token",
            "Effect": "Allow",
            "Action": "bedrock-agentcore:GetResourceOauth2Token",
            "Resource": identity_resources
            + [f"{base}:token-vault/default/oauth2credentialprovider/{n}" for n in oauth2],
        })
    if api_key:
        statements.append({
            "Sid": "ToolAuthApiKey",
            "Effect": "Allow",
            "Action": "bedrock-agentcore:GetResourceApiKey",
            "Resource": identity_resources
            + [f"{base}:token-vault/default/apikeycredentialprovider/{n}" for n in api_key],
        })
    statements.append({
        "Sid": "ToolAuthVaultSecrets",
        "Effect": "Allow",
        "Action": ["secretsmanager:GetSecretValue"],
        "Resource": [f"{secret_base}/oauth2/{n}-*" for n in oauth2]
        + [f"{secret_base}/apikey/{n}-*" for n in api_key],
    })
    return statements


def _agent_tool_statements(spec: AgentSpec, ctx: RoleContext) -> list[dict[str, Any]]:
    """Agents-as-tools (strands-builder SB-2): invoke exactly the runtimes the spec
    offers as tools — the runtime ARN and the endpoint its production traffic uses.

    The ARNs are server-owned (``services.agent_tools`` resolves them from the ledger
    on save and again in the generate stage, before this runs), and only ARNs of
    this account and region are granted. ``InvokeAgentRuntimeForUser`` joins for a
    target that needs ``runtimeUserId`` (its own Gateway / identity tools).
    """
    prefix = f"arn:{ctx.partition}:bedrock-agentcore:{ctx.region}:{ctx.account_id}:runtime/"
    refs = [
        ref for ref in spec.agent_tools
        if ref.runtime_arn and ref.runtime_arn.startswith(prefix)
    ]
    if not refs:
        return []

    def resources(group: list) -> list[str]:
        arns: set[str] = set()
        for ref in group:
            arns.add(ref.runtime_arn)
            arns.add(f"{ref.runtime_arn}/runtime-endpoint/{ref.qualifier}")
        return sorted(arns)

    statements: list[dict[str, Any]] = [{
        "Sid": "AgentToolsInvoke",
        "Effect": "Allow",
        "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
        "Resource": resources(refs),
    }]
    for_user = [ref for ref in refs if ref.runtime_user]
    if for_user:
        statements.append({
            "Sid": "AgentToolsInvokeForUser",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:InvokeAgentRuntimeForUser"],
            "Resource": resources(for_user),
        })
    return statements


def _preset_kb_oauth_statements(spec: AgentSpec, ctx: RoleContext) -> list[dict[str, Any]]:
    """The exact OAuth2-credential-provider grants the harness devguide lists for an
    OAuth-protected gateway ("Execution role policy → OAuth2 credential provider",
    read 2026-09-12), instantiated for the KB gateway's real provider.

    Nothing else: no `GetResourceApiKey` (no API-key provider is attached), no
    `GetWorkloadAccessToken*` (the devguide's OAuth policy names only
    `GetResourceOauth2Token`), no family-wide `bedrock-agentcore-identity!*` secret,
    and no direct `bedrock:Retrieve` / `AgenticRetrieveStream` — a harness reaches the
    KB through the gateway, whose connector role performs the retrieval.
    """
    provider = oauth_provider_name(ctx.oauth_provider_arn)
    if not provider:
        raise ValueError(
            "the workspace resource map has no oauth_provider_arn — the KB gateway's "
            "OAuth2 credential provider is required to scope the preset's grants"
        )
    base = f"arn:{ctx.partition}:bedrock-agentcore:{ctx.region}:{ctx.account_id}"
    harness_name = spec.name.replace("-", "_")  # deployer/harness.py harnessName
    return [
        {
            "Sid": "AgentCoreOAuth2TokenVaultDefault",
            "Effect": "Allow",
            "Action": "bedrock-agentcore:GetResourceOauth2Token",
            "Resource": [
                f"{base}:token-vault/default",
                f"{base}:workload-identity-directory/default",
                f"{base}:workload-identity-directory/default/workload-identity/"
                f"harness_{harness_name}-*",
            ],
        },
        {
            "Sid": "AgentCoreOAuth2TokenVaultPerProvider",
            "Effect": "Allow",
            "Action": "bedrock-agentcore:GetResourceOauth2Token",
            "Resource": ctx.oauth_provider_arn,
        },
        {
            "Sid": "AgentCoreOAuth2Secret",
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": (
                f"arn:{ctx.partition}:secretsmanager:{ctx.region}:{ctx.account_id}:secret:"
                f"bedrock-agentcore-identity!default/oauth2/{provider}-*"
            ),
        },
    ]


def policy_document(
    spec: AgentSpec,
    ctx: RoleContext,
    *,
    system_preset: bool = False,
    inbound_jwt: bool = False,
) -> dict:
    """The capability policy for one agent.

    Statements appear only when the spec calls for them. Sids match the shared CDK
    role so the two can be diffed. ``system_preset`` selects the narrow, devguide-
    exact OAuth grants for a preset's KB gateway instead of the generic identity
    family (see `_preset_kb_oauth_statements`); ordinary agents are unchanged.
    ``inbound_jwt`` is the RESOLVED inbound mode (spec > workspace default): a
    JWT-mode runtime exchanges the caller's bearer for a workload token via
    ``GetWorkloadAccessTokenForJWT``, so its role must allow that call.
    """
    statements: list[dict[str, Any]] = []

    # ---- models: scoped to the configured id(s); an MCP server is a tool that
    # calls no model, so it gets none ----
    if spec.protocol != "mcp":
        statements.append({
            "Sid": "BedrockModels",
            "Effect": "Allow",
            "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            # an application inference profile authorizes against the profile and the
            # models it routes to — the latter are already the spec's own resources
            "Resource": list(dict.fromkeys(
                [*allowed_model_resources(spec, ctx), *ctx.inference_profile_arns]
            )),
        })

    if spec.protocol != "mcp" and uses_mantle(spec):
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
            "Resource": [f"arn:{ctx.partition}:bedrock-mantle:*:{ctx.account_id}:project/*"],
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
    # NOTE: the memory defaults to a shared singleton, so this scopes to one
    # resource but does NOT give per-agent memory isolation there. Partitioning is
    # done by folding the agent id into the actor id (see services/memory.py::
    # scoped_actor). A spec that pins its own memory (spec.memory.memory_id) gets
    # the grant scoped to that memory instead.
    if spec.memory.short_term or spec.memory.long_term:
        selected_memory = spec.memory.memory_id or ctx.memory_id
        memory_resource = (
            f"arn:{ctx.partition}:bedrock-agentcore:{ctx.region}:{ctx.account_id}:memory/{selected_memory}"
            if selected_memory else "*"
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
    # Tool-level outbound auth: exact per-Connection grants, additive to (and
    # independent of) the family-wide gateway grant below.
    statements.extend(_tool_auth_statements(spec, ctx))
    statements.extend(_agent_tool_statements(spec, ctx))
    if system_preset and spec.knowledge_bases:
        statements.extend(_preset_kb_oauth_statements(spec, ctx))
    elif _uses_gateway(spec):
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
                f"arn:{ctx.partition}:secretsmanager:{ctx.region}:{ctx.account_id}"
                ":secret:bedrock-agentcore-identity!*"
            ],
        })

    # ---- SigV4 (AWS_IAM) gateways: scoped to the referenced gateway ids ----
    # A Harness reaches an AWS_IAM Gateway by signing with this role, which the
    # Gateway authorizes as bedrock-agentcore:InvokeGateway (harness devguide,
    # "Execution role policy → AgentCore Gateway"). The spec carries only the id;
    # the region segment stays wildcarded because a Registry record may name a
    # Gateway in another region of the account. Granting it for an OAuth Gateway
    # too is inert: that Gateway authorizes the bearer token, not the caller's IAM.
    gateway_ids = _gateway_ids(spec)
    if gateway_ids:
        statements.append({
            "Sid": "AgentCoreGatewayInvoke",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:InvokeGateway"],
            "Resource": [
                f"arn:{ctx.partition}:bedrock-agentcore:*:{ctx.account_id}:gateway/{gateway_id}"
                for gateway_id in gateway_ids
            ],
        })

    # ---- inbound JWT ----
    # A JWT-mode runtime exchanges the validated inbound bearer for a workload
    # access token (GetWorkloadAccessTokenForJWT) on the runtime's own workload
    # identity — needed by agents whose role predates the Identity service-
    # linked role, and by the P2 OBO path. Skipped when the family-wide gateway
    # grant above already carries the action; scoped to the directory plus the
    # runtime's OWN identity (its name embeds the runtime id, unknown at
    # provision time — ``own_workload_identity_arn``), matching the devguide's
    # GetAgentAccessToken policy shape.
    if inbound_jwt and not any(
        s.get("Sid") == "AgentCoreWorkloadIdentity" for s in statements
    ):
        base = f"arn:aws:bedrock-agentcore:{ctx.region}:{ctx.account_id}"
        statements.append({
            "Sid": "InboundJwtWorkloadToken",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:GetWorkloadAccessToken",
                "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
            ],
            "Resource": [
                f"{base}:workload-identity-directory/default",
                own_workload_identity_arn(spec, ctx),
            ],
        })

    # ---- builtin tools ----
    # The full session-level action sets (use the tool, not manage its resources:
    # no Create*/Delete*), so code driving the tool itself — e.g. a byoc agent's
    # CDP automation stream, live view, screenshots — is not denied midway.
    builtins = _builtin_names(spec)
    # SB-3: the sandboxed tools and Code Interpreter custom tools open sessions too
    if BUILTIN_CODE_INTERPRETER in builtins or uses_code_interpreter(spec):
        statements.append({
            "Sid": "AgentCoreCodeInterpreter",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:InvokeCodeInterpreter",
                "bedrock-agentcore:StartCodeInterpreterSession",
                "bedrock-agentcore:StopCodeInterpreterSession",
                "bedrock-agentcore:GetCodeInterpreterSession",
                "bedrock-agentcore:ListCodeInterpreterSessions",
                "bedrock-agentcore:GetCodeInterpreter",
                "bedrock-agentcore:ListCodeInterpreters",
            ],
            "Resource": "*",
        })
    if BUILTIN_BROWSER in builtins:
        statements.append({
            "Sid": "AgentCoreBrowser",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:StartBrowserSession",
                "bedrock-agentcore:StopBrowserSession",
                "bedrock-agentcore:GetBrowserSession",
                "bedrock-agentcore:ListBrowserSessions",
                "bedrock-agentcore:UpdateBrowserStream",
                "bedrock-agentcore:ConnectBrowserAutomationStream",
                "bedrock-agentcore:ConnectBrowserLiveViewStream",
                "bedrock-agentcore:GetBrowser",
                "bedrock-agentcore:ListBrowsers",
            ],
            "Resource": "*",
        })

    # ---- container image pull ----
    byoc_kind = spec.byoc.artifact_kind if spec.byoc else None
    # a Node stdio MCP server is built into the shared repo on the Node bridge base
    node_stdio = (
        byoc_kind == "code_zip" and spec.mcp is not None
        and spec.mcp.stdio_wrapper is not None and spec.mcp.stdio_wrapper.base == "node"
    )
    if (spec.method == "container" or node_stdio
            or byoc_kind in ("container_source", "container_image")):
        # byoc container_image may name any repo in this account; scope to it
        # rather than the shared launchpad-agents repo
        if byoc_kind == "container_image" and spec.byoc and spec.byoc.image_uri:
            repo_name = spec.byoc.image_uri.split(".amazonaws.com/", 1)[1]
            repo_name = repo_name.split("@", 1)[0].rsplit(":", 1)[0]
            repo_arn = (
                f"arn:{ctx.partition}:ecr:{ctx.region}:{ctx.account_id}:repository/{repo_name}"
            )
        else:
            repo_arn = ctx.ecr_repo_arn
        repo_arns = [repo_arn]
        if byoc_kind != "container_image":
            repo_arns += [arn for arn in ctx.extra_ecr_repo_arns if arn != repo_arn]
        statements.append({
            "Sid": "EcrPull",
            "Effect": "Allow",
            "Action": ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"],
            "Resource": repo_arns,
        })
        statements.append({
            # UNSCOPABLE: ecr:GetAuthorizationToken takes no resource by design.
            "Sid": "EcrAuth",
            "Effect": "Allow",
            "Action": ["ecr:GetAuthorizationToken"],
            "Resource": "*",
        })

    # ---- VPC-mode harness: the managed image comes from private ECR ----
    # On the public network a harness pulls its managed container from ECR Public;
    # in VPC mode it pulls `harness-<region>` from a private ECR repository owned by
    # an AWS service account, WITH THIS ROLE. Without the grant every session fails
    # to start ("no basic auth credentials" in the runtime log, then InvokeHarness
    # "Runtime initialization time exceeded") — measured live. The repository
    # account is not ours, so it is wildcarded; region and repo prefix are not.
    if spec.method == "harness" and spec.network is not None:
        statements.append({
            "Sid": "HarnessManagedImagePull",
            "Effect": "Allow",
            "Action": [
                "ecr:BatchGetImage",
                "ecr:GetDownloadUrlForLayer",
                "ecr:BatchCheckLayerAvailability",
            ],
            "Resource": [f"arn:{ctx.partition}:ecr:{ctx.region}:*:repository/harness-*"],
        })
        statements.append({
            # UNSCOPABLE: ecr:GetAuthorizationToken takes no resource by design.
            "Sid": "HarnessManagedImageToken",
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
                f"arn:{ctx.partition}:s3:::{ctx.artifacts_bucket}/{prefix}*" for prefix in prefixes
            ],
        })
        statements.append({
            "Sid": "SkillBundleList",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": [f"arn:{ctx.partition}:s3:::{ctx.artifacts_bucket}"],
            "Condition": {"StringLike": {"s3:prefix": [f"{p}*" for p in prefixes]}},
        })

    # ---- managed knowledge bases: scoped to the attached ones ----
    # A system preset reaches its KBs only through the KB gateway (the connector's
    # own role retrieves), so it gets no direct retrieval grant at all.
    if spec.knowledge_bases and not system_preset:
        statements.append({
            "Sid": "ManagedKbRetrieval",
            "Effect": "Allow",
            "Action": ["bedrock:Retrieve", "bedrock:GetKnowledgeBase"],
            "Resource": [
                f"arn:{ctx.partition}:bedrock:{ctx.region}:{ctx.account_id}:knowledge-base/{kb.kb_id}"
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

    # ---- routed configuration bundles (config-bundle A/B experiments) ----
    # The generated agent reads its system prompt and tool descriptions from
    # BedrockAgentCoreContext.get_config_bundle(), and the SDK resolves the baggage
    # reference by calling GetConfigurationBundleVersion **with this role**. Without
    # this statement a routed bundle fails with AccessDenied inside the runtime and
    # the whole invocation 500s — measured live; the shared CDK role has the grant
    # (base_stack ABTestOrchestration) but a per-agent role did not, so every
    # experiment on a per-agent-role agent was broken.
    #
    # Read-only, and scoped to this account+region: bundle names are generated per
    # experiment, so there is no narrower resource to name.
    if spec.method != "harness":
        statements.append({
            "Sid": "ConfigurationBundleRead",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:GetConfigurationBundle",
                "bedrock-agentcore:GetConfigurationBundleVersion",
            ],
            "Resource": [
                f"arn:{ctx.partition}:bedrock-agentcore:{ctx.region}:{ctx.account_id}"
                ":configuration-bundle/*"
            ],
        })

    # ---- A2A agents legitimately invoke other runtimes ----
    if spec.protocol == "a2a":
        statements.append({
            "Sid": "A2AInvokePeerRuntimes",
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
            "Resource": [
                f"arn:{ctx.partition}:bedrock-agentcore:{ctx.region}:{ctx.account_id}:runtime/*"
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
            f"arn:{ctx.partition}:logs:{ctx.region}:{ctx.account_id}:log-group:"
            "/aws/bedrock-agentcore/runtimes/*",
            f"arn:{ctx.partition}:logs:{ctx.region}:{ctx.account_id}:log-group:"
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

    for build in EXTRA_STATEMENT_BUILDERS:
        statements.extend(build(spec, ctx) or [])

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

def fs_policy_document(spec: AgentSpec, agent_id: str | None = None,
                       partition: str = "aws") -> dict | None:
    """Inline policy granting mount access to the BYO access points.

    S3 Files APs embed the file-system ARN (strip '/access-point/…'); EFS APs don't —
    Resource '*' scoped by the AccessPointArn condition.

    The devguide's example (one combined conditioned statement with
    ClientMount/ClientWrite/GetAccessPoint) is WRONG and incomplete:
    - GetAccessPoint authorizes on the access-point ARN and does not carry the
      s3files:AccessPointArn condition key → its own unconditioned statement;
    - AgentCore's create/update validation ALSO requires ListMountTargets on the
      file system (undocumented).

    S3 bucket mounts (storage catalog kind ``s3bucket``) add prefix-scoped S3 statements;
    a per-session one is scoped to ``${aws:AgentCoreRuntimeSourceSid}`` under
    ``<entry prefix><agent_id>/``, so ``agent_id`` is required for those.
    """
    # inline (read-write) and catalog (per-mode) mounts, one builder (storage-mounts §4.3)
    from app.services.storage_mounts import storage_iam_statements

    statements: list[dict] = storage_iam_statements(
        spec, agent_id=agent_id, partition=partition)
    if not statements:
        return None
    return {"Version": "2012-10-17", "Statement": statements}


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectBoundary:
    """What an isolated project's agent role carries on top of its policies (§10.3)."""

    project_id: str
    workspace_id: str
    policy_arn: str
    label: str = ""  # `boundary-v<N>`, recorded as `agents.isolation_applied`

    def tags(self) -> list[dict[str, str]]:
        from app.services.project_iam import (
            ISOLATED_TAG_VALUE,
            ISOLATION_TAG_KEY,
            PROJECT_TAG_KEY,
        )
        from app.services.workspace_iam import WORKSPACE_TAG_KEY

        return [
            {"Key": PROJECT_TAG_KEY, "Value": self.project_id},
            {"Key": WORKSPACE_TAG_KEY, "Value": self.workspace_id},
            {"Key": ISOLATION_TAG_KEY, "Value": ISOLATED_TAG_VALUE},
        ]


def ensure_role(
    iam: Any,
    agent: Agent,
    spec: AgentSpec,
    ctx: RoleContext,
    log: Callable[[str], None] = lambda _m: None,
    runtime_arn: str | None = None,
    boundary: ProjectBoundary | None = None,
) -> str:
    """Create or reconcile the agent's role; return its ARN.

    Idempotent, because `resume_pending_jobs()` re-enters the provision stage. An
    existing role of the same name is **adopted** rather than treated as an error: a
    previous delete may have half-failed, and failing here would wedge re-creating an
    agent under a name that was used before.

    `boundary` (isolated projects only) is set at create — the spoke role refuses an
    `isolated`-tagged CreateRole without one — and re-applied with the tags on adopt,
    so a role made before its project was isolated gets it too.
    """
    name = role_name_for(agent.name, agent.id)
    trust = json.dumps(trust_policy(ctx, runtime_arn))
    tags = [
        {"Key": MANAGED_TAG_KEY, "Value": agent.id},
        {"Key": "launchpad:managed", "Value": "true"},
    ]
    extra: dict[str, Any] = {}
    if boundary is not None:
        tags += boundary.tags()
        extra["PermissionsBoundary"] = boundary.policy_arn
    try:
        created = iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=trust,
            Description=f"Launchpad execution role for agent {agent.name} ({agent.id})",
            Tags=tags,
            **extra,
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
        if boundary is not None:
            # tags first: they are what marks the role isolated for the spoke's
            # conditions and for drift detection
            iam.tag_role(RoleName=name, Tags=boundary.tags())
            iam.put_role_permissions_boundary(
                RoleName=name, PermissionsBoundary=boundary.policy_arn
            )
    if boundary is not None:
        log(f"permissions boundary {boundary.policy_arn.rsplit('/', 1)[-1]}"
            f"{' ' + boundary.label if boundary.label else ''} on {name}")

    iam.put_role_policy(
        RoleName=name,
        PolicyName=capability_policy_name(agent.name),
        PolicyDocument=json.dumps(
            policy_document(
                spec,
                ctx,
                system_preset=bool(getattr(agent, "system_key", None)),
                inbound_jwt=_resolves_to_jwt(agent),
            )
        ),
    )
    _sync_fs_policy(iam, name, agent, spec, log, ctx.partition)
    return role_arn


def _resolves_to_jwt(agent: Agent) -> bool:
    """Whether this agent's CURRENT deploy resolves to inbound JWT.

    The provision stage runs before the deploy stage writes the snapshot, so
    this resolves afresh (spec > workspace default) through the service helper.
    Late import: services.inbound_auth imports the ledger models, and a top-
    level import from here would cycle through services.workspace.
    """
    from app.core.db import SessionLocal
    from app.services import inbound_auth as inbound_auth_service

    db = SessionLocal()
    try:
        return inbound_auth_service.resolve_for_agent(agent, db).mode == "jwt"
    finally:
        db.close()


def _sync_fs_policy(
    iam: Any, role_name: str, agent: Agent, spec: AgentSpec, log: Callable[[str], None],
    partition: str = "aws",
) -> None:
    """Attach the BYO-mount policy, or drop a stale one when the mounts were removed
    on re-publish."""
    policy = fs_policy_document(spec, agent.id, partition)
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
    except Exception as exc:  # noqa: BLE001 — absent on most agents, nothing to clean
        if not is_not_found(exc):
            message = (
                f"stale inline policy {name} could not be removed from role "
                f"{role_name}: {cleanup_error(exc)}"
            )
            logger.warning(message)
            log(message)


def provision_execution_role(
    agent: Agent,
    spec: AgentSpec,
    settings: Any,
    workspace: WorkspaceContext,
    log: Callable[[str], None] = lambda _m: None,
    iam: Any = None,
) -> tuple[str, str]:
    """The provision-stage entry point shared by all three deployers.

    Returns `(role_arn, detail)`. Falls back to the shared role when per-agent roles
    are switched off, so the two paths differ in one place rather than three.
    `settings` carries the hub-global toggle; `workspace` carries the environment
    the role is created in.
    """
    if not settings.per_agent_execution_roles:
        _refuse_shared_role_if_isolated(getattr(agent, "project_id", None))
        arn = shared_role_arn(workspace)
        if not arn:
            raise RuntimeError(
                "execution_role_arn missing from this workspace's resource map — "
                "run its bootstrap"
            )
        log(f"per-agent roles disabled — reusing shared execution role {arn}")
        return arn, "iam role reused · launchpad-base (shared)"

    if iam is None:
        iam = workspace.client("iam")
    project_id = getattr(agent, "project_id", None)
    ctx = role_context(workspace, project_id)
    from app.deployer.model_profile import profile_arns

    if profiles := profile_arns(project_id, spec, workspace, log):
        ctx = replace(ctx, inference_profile_arns=tuple(profiles.values()))
    boundary = _project_boundary(agent, spec, workspace, iam, log)
    arn = ensure_role(iam, agent, spec, ctx, log, boundary=boundary)
    name = role_name_for(agent.name, agent.id)
    # cost-tags §1.2: the role carries the agent's cost tags, set on every provision
    # (create and update); best-effort, never fails the deploy. Late import: cycle.
    from app.services import cost_tags

    cost_tags.tag_quietly(_IamWorkspace(workspace, iam),
                          cost_tags.entry(cost_tags.IAM_ROLE, name=name),
                          cost_tags.agent_tags(agent, workspace.id), log)
    detail = f"iam role · {name}"
    if fs_policy_document(spec, agent.id, ctx.partition):
        detail += f" (+ {fs_policy_name(agent.name)})"
    if boundary is not None:
        detail += f" · boundary {boundary.label}"
        _record_isolation_applied(agent, boundary.label)
    return arn, detail


class _IamWorkspace:
    """The workspace with the provision stage's own IAM client (tests inject one)."""

    def __init__(self, workspace: WorkspaceContext, iam: Any) -> None:
        self._workspace = workspace
        self._iam = iam

    def client(self, service: str, *args: Any, **kwargs: Any) -> Any:
        if service == "iam":
            return self._iam
        return self._workspace.client(service, *args, **kwargs)


def _refuse_shared_role_if_isolated(project_id: str | None) -> None:
    """§10.3: the shared execution role is never used by an isolated project's agent
    (it carries no boundary). Late import: project_iam imports the ledger models."""
    from app.services.project_iam import isolated_project

    if isolated_project(project_id):
        raise AppError(
            "project.isolation_requires_per_agent_role",
            f"project '{project_id}' is isolated: its agents need their own execution "
            "role (per-agent roles are switched off, or the runtime reports no role)",
            {"project_id": project_id},
            status_code=409,
        )


def _project_boundary(
    agent: Agent, spec: AgentSpec, workspace: WorkspaceContext, iam: Any,
    log: Callable[[str], None],
) -> ProjectBoundary | None:
    """The boundary for an isolated project's agent, made current for this spec;
    None for every other agent (no IAM call, no change)."""
    from app.services import project_iam

    state = project_iam.boundary_for_agent(agent, spec, workspace, iam, log)
    if state is None:
        return None
    return ProjectBoundary(
        project_id=agent.project_id, workspace_id=workspace.id,
        policy_arn=state.arn, label=state.label,
    )


def _record_isolation_applied(agent: Agent, label: str) -> None:
    """`agents.isolation_applied` — on the caller's object and in the ledger, since the
    deployer's session may not flush the agent row after this stage."""
    from sqlalchemy import update

    from app.core.db import SessionLocal

    agent.isolation_applied = label
    if not getattr(agent, "id", None):
        return
    db = SessionLocal()
    try:
        db.execute(update(Agent).where(Agent.id == agent.id).values(isolation_applied=label))
        db.commit()
    finally:
        db.close()


def delete_execution_role(
    agent: Agent,
    settings: Any,
    workspace: WorkspaceContext,
    log: Callable[[str], None] = lambda _m: None,
    iam: Any = None,
) -> bool:
    """Delete the agent's role, if it has one. Never raises."""
    if not settings.per_agent_execution_roles:
        return True  # the shared role is not ours to delete
    if iam is None:
        iam = workspace.client("iam")
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
#
# The wording a brand-new role actually produces was only learned from a live zip
# deploy (2026-08-04), and it matched none of the guesses above:
#
#   ValidationException: Role validation failed for 'arn:aws:iam::…:role/…'. Please
#   verify that the role exists and its trust policy allows assumption by this service
#
# The trust policy was byte-identical to the shared role's, which works — so this
# phrasing means "not visible yet", not "misconfigured". It is kept in the list even
# though a genuinely broken trust policy produces it too: retrying costs a minute and
# the error still surfaces afterwards, whereas not retrying fails every first deploy.
_PROPAGATION_MARKERS = (
    "missing required permissions",
    "is not authorized to perform: sts:assumerole",
    "unable to assume",
    "cannot be assumed",
    "accessdenied",
    "access denied",
    "role validation failed",
    "trust policy allows assumption",
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
