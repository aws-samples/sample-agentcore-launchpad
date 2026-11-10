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

No statement names a Bedrock model: the CN build authenticates to Global Bedrock with
an API key fetched from AgentCore Identity, not with this role.

The IAM client is injected by the caller: `container.py::_stage_provision` already
takes one for exactly this reason, and it keeps the derivation testable.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from app.core import naming
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import aws_clients
from app.services.workspace import WorkspaceContext

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
    partition: str = "aws"
    dns_suffix: str = "amazonaws.com"  # cn-lint: allow
    # --- CN memory/KB (N6) ---
    # The workspace's self-hosted memory substrate, from its resource map (written by
    # the `memory` bootstrap stage). All empty when the substrate does not exist, in
    # which case no memory/KB statement is emitted (ruling R2).
    memory_store_id: str = ""
    memory_bucket: str = ""
    memory_prefix: str = "memory"
    memory_queue_arn: str = ""
    memory_domain_arn: str = ""
    # --- end CN memory/KB (N6) ---

    def arn(
        self, service: str, resource: str, region: str | None = None, account: str | None = None
    ) -> str:
        reg = self.region if region is None else region
        acct = self.account_id if account is None else account
        return f"arn:{self.partition}:{service}:{reg}:{acct}:{resource}"


def role_context(workspace: WorkspaceContext) -> RoleContext:
    """Build a `RoleContext` from the workspace the agent is deployed into."""
    resources = workspace.resources or {}
    repo = resources.get("ecr_repo", naming.default_ecr_repo())
    return RoleContext(
        account_id=workspace.account_id,
        region=workspace.region,
        artifacts_bucket=resources.get("artifacts_bucket", ""),
        ecr_repo_arn=workspace.arn("ecr", f"repository/{repo}"),
        partition=workspace.partition,
        dns_suffix=workspace.dns_suffix,
        # --- CN memory/KB (N6) ---
        memory_store_id=str(resources.get("memory_id") or ""),
        memory_bucket=str(resources.get("memory_bucket") or resources.get("artifacts_bucket", "")),
        memory_prefix=str(resources.get("memory_prefix") or "memory"),
        memory_queue_arn=str(resources.get("memory_queue_arn") or ""),
        memory_domain_arn=str(resources.get("memory_os_domain_arn") or ""),
        # --- end CN memory/KB (N6) ---
    )


def live_runtime_role_arn(
    runtime_detail: dict[str, Any] | None, workspace: WorkspaceContext
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
    """
    live = (runtime_detail or {}).get("roleArn") or ""
    return live or shared_role_arn(workspace)


def shared_role_arn(workspace: WorkspaceContext) -> str:
    """The pre-existing shared role, still used by candidate versions and as the
    fallback when per-agent roles are turned off."""
    return (workspace.resources or {}).get("execution_role_arn", "")


# ---------------------------------------------------------------------------
# naming
# ---------------------------------------------------------------------------

def role_name_for(agent_name: str, agent_id: str, env_name: str | None = None) -> str:
    """`launchpad-agent-{name}-{id8}` (N11: `-{env}` appended under a non-empty
    `Settings.env_name`), inside IAM's 64-character limit.

    The id suffix is not decoration: agent names are user-supplied, so truncating
    the name alone would collide two agents whose names share a prefix. Keeping the
    id *in the name* (rather than only in a tag) also lets an operator reading the
    IAM console map a role back to a ledger row. The env suffix comes last so the
    name still falls under the `launchpad-agent-*` wildcards (ops role, FGAC mapping).
    """
    env = naming.current_env_name() if env_name is None else naming.validate_env_name(env_name)
    suffix = (agent_id or "")[:_ID_SUFFIX_LEN] or "00000000"
    room = _ROLE_NAME_MAX - len(_ROLE_PREFIX) - 1 - len(suffix) - (len(env) + 1 if env else 0)
    stem = _SANITISE_RE.sub("-", agent_name or "agent").strip("-")[:room] or "agent"
    return naming.suffixed(f"{_ROLE_PREFIX}{stem}-{suffix}", env)


def agent_role_pattern(env_name: str | None = None) -> str:
    """IAM `role/...` wildcard matching THIS environment's per-agent roles — the FGAC
    role-mapping entry and the bootstrap grants use it. `role/launchpad-agent-*` by
    default; `role/launchpad-agent-*-t1` under env name `t1`, so one environment's
    OpenSearch mapping does not admit another's agents."""
    env = naming.current_env_name() if env_name is None else naming.validate_env_name(env_name)
    return naming.suffixed(f"role/{_ROLE_PREFIX}*", env)


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
            "Principal": {"Service": aws_clients.service_principal("bedrock-agentcore")},
            "Action": "sts:AssumeRole",
            "Condition": conditions,
        }],
    }


# ---------------------------------------------------------------------------
# capability derivation
# ---------------------------------------------------------------------------

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
        # KBs ride the direct retrieval tools; the family-wide grant stays for compatibility
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


def _a2a_peer_arns(spec: AgentSpec) -> list[str]:
    """Runtime ARNs of the A2A peers the spec mounts (``type="a2a"``, N11 r3-C) — sorted,
    de-duplicated; the schema already validated each as a bedrock-agentcore runtime ARN."""
    return sorted({
        str((tool.config or {}).get("runtime_arn"))
        for tool in spec.tools
        if tool.type == "a2a" and (tool.config or {}).get("runtime_arn")
    })


# Length of the random hex suffix ``deployer.zip_runtime.sanitize_runtime_name`` appends
# to ``runtime_name_base``; the single source for every ``<base>_<hex>`` shape check.
RUNTIME_SUFFIX_HEX_LEN = 6


def runtime_name_base(agent_name: str) -> str:
    """The deterministic part of an agent's Runtime name.

    ``deployer.zip_runtime.sanitize_runtime_name`` appends ``_<6 hex>`` to this
    for uniqueness; it lives here because the workload-identity scope below
    must agree with it exactly. The agent name is immutable after create (the
    re-publish route refuses a rename), so the base never drifts from the
    runtime that already exists.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", agent_name).strip("_")[:40] or "agent"


def runtime_name_pattern(agent_name: str) -> re.Pattern[str]:
    """Exact shape of the Runtime names ``sanitize_runtime_name(agent_name)`` can produce:
    ``^<runtime_name_base(agent_name)>_[0-9a-f]{6}$`` — the base, one ``_``, exactly
    ``RUNTIME_SUFFIX_HEX_LEN`` hex characters, nothing else.

    Selectors that look a Runtime up by agent name must use this, never a bare
    ``startswith(base + "_")``: bases are not unique by construction (``launchpad_eval_worker``
    is a strict prefix of the ``t1`` environment's ``launchpad_eval_worker_t1``), so a prefix
    test on the default environment's base also matches ``launchpad_eval_worker_t1_bd4aba``
    (N12-bug12 — a default-env teardown dry run selected t1's evaluation worker).
    """
    base = re.escape(runtime_name_base(agent_name))
    return re.compile(rf"^{base}_[0-9a-f]{{{RUNTIME_SUFFIX_HEX_LEN}}}$")


def own_workload_identity_arn(spec: AgentSpec, ctx: RoleContext) -> str:
    """The agent's OWN auto-created workload identity, as an IAM resource.

    The Runtime names that identity after the runtime id — ``<runtimeName>-
    <10-char suffix>`` (deployer/return_url.py, live-verified 2026-09-19;
    evidence docs/identity-e2e-evidence-p2.md) — and the runtime name
    is ``<base>_<6 hex>``. Neither random part is known at provision time,
    so the single-character ``?`` wildcard pins the 6-hex segment exactly: the
    pattern matches this agent's runtimes, not another agent whose name merely
    starts with the same characters (``a`` vs ``a_b``).
    """
    base = ctx.arn("bedrock-agentcore", "").rstrip(":")
    return (
        f"{base}:workload-identity-directory/default/workload-identity/"
        f"{runtime_name_base(spec.name)}_??????-*"
    )


def _tool_auth_statements(spec: AgentSpec, ctx: RoleContext) -> list[dict[str, Any]]:
    """Per-Connection grants for tools carrying ``ToolRef.auth``.

    Follows the devguide-exact shape: the
    token action on the token vault + the workload-identity directory + the exact
    provider ARNs, and ``GetSecretValue`` on the vault's fixed secret prefix for
    exactly those Connection names — never the family-wide
    ``bedrock-agentcore-identity!*``. An agent with no ``auth`` tools gets nothing
    from here (grant only when used).
    """
    wanted = {(tool.auth.connection, tool.auth.kind) for tool in spec.tools if tool.auth}
    if not wanted:
        return []
    base = ctx.arn("bedrock-agentcore", "").rstrip(":")
    secret_base = (
        ctx.arn("secretsmanager", "secret:bedrock-agentcore-identity!default")
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


def _model_credential_statements(spec: AgentSpec, ctx: RoleContext) -> list[dict[str, Any]]:
    """Every agent: exchange the Runtime-injected workload token for the Global Bedrock
    API key — ``GetResourceApiKey`` on the workspace's ``model_credential`` provider —
    the only way a runtime in aws-cn reaches the Global models (spec C3/C4).

    Same devguide-exact shape as ``_tool_auth_statements``: token vault + directory +
    this agent's OWN workload identity + the exact provider, and ``GetSecretValue`` on
    that provider's vault secret prefix only (never the family-wide
    ``bedrock-agentcore-identity!*``). ``GetWorkloadAccessToken`` on the same identity
    resources backs the templates' last-resort self-mint (``LAUNCHPAD_WORKLOAD_NAME``) for a
    request that arrives without the injected token — which Identity refuses for a
    runtime-linked identity ("linked to a service"; N10v3-bug5 live correction), so the
    grant is harmless but the token has to come from the caller's ``runtimeUserId``.
    """
    base = ctx.arn("bedrock-agentcore", "").rstrip(":")
    providers = model_credential_providers(spec.model_credential)
    return [
        {
            "Sid": "ModelCredentialApiKey",
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:GetResourceApiKey",
                "bedrock-agentcore:GetWorkloadAccessToken",
            ],
            "Resource": [
                f"{base}:token-vault/default",
                f"{base}:workload-identity-directory/default",
                own_workload_identity_arn(spec, ctx),
                *[f"{base}:token-vault/default/apikeycredentialprovider/{name}"
                  for name in providers],
            ],
        },
        {
            "Sid": "ModelCredentialVaultSecret",
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [
                ctx.arn(
                    "secretsmanager",
                    f"secret:bedrock-agentcore-identity!default/apikey/{name}-*",
                )
                for name in providers
            ],
        },
    ]


# N-R6: every platform model credential provider is named `launchpad-*` (PROVIDERS); a
# runtime fetches the one its model needs — an agent on a SiliconFlow model, a judge on
# any catalog model — so the grant covers the family, plus the spec's own provider when it
# is a custom name outside it.
MODEL_CREDENTIAL_PROVIDER_PATTERN = "launchpad-*"


def model_credential_providers(spec_credential: str) -> list[str]:
    names = [spec_credential] if not spec_credential.startswith("launchpad-") else []
    return names + [MODEL_CREDENTIAL_PROVIDER_PATTERN]


# --- CN memory/KB (N6) ---
def _memory_statements(spec: AgentSpec, ctx: RoleContext) -> list[dict[str, Any]]:
    """S3 snapshot prefix, extraction queue and the memory index — only for agents with
    memory on, in a workspace whose substrate exists (ruling R2). The Titan key grant is
    the unconditional ModelCredentialApiKey (N2); nothing here names a model.

    Scope: this agent's store prefix `<prefix>/<store>/*` (a spec-pinned store or the
    workspace's), the one extraction queue, and this environment's memory index
    (`launchpad-memory` / `launchpad-<env>-memory`, D17) of the shared domain — never the
    bucket root, the queue family or the whole domain.
    """
    if not (spec.memory.short_term or spec.memory.long_term) or not ctx.memory_domain_arn:
        return []
    store = spec.memory.memory_id or ctx.memory_store_id
    if not store or not ctx.memory_bucket:
        return []
    prefix = f"{ctx.memory_prefix.strip('/') or 'memory'}/{store}"
    statements: list[dict[str, Any]] = [
        {
            "Sid": "MemorySnapshotObjects",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            "Resource": [
                ctx.arn("s3", f"{ctx.memory_bucket}/{prefix}/*", region="", account="")
            ],
        },
        {
            "Sid": "MemorySnapshotList",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": [ctx.arn("s3", ctx.memory_bucket, region="", account="")],
            "Condition": {"StringLike": {"s3:prefix": [f"{prefix}/*"]}},
        },
    ]
    if ctx.memory_queue_arn:
        statements.append({
            "Sid": "MemoryExtractionQueue",
            "Effect": "Allow",
            "Action": ["sqs:SendMessage"],
            "Resource": [ctx.memory_queue_arn],
        })
    statements.append({
        "Sid": "MemorySearch",
        "Effect": "Allow",
        "Action": ["es:ESHttpGet", "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpHead"],
        "Resource": [f"{ctx.memory_domain_arn}/{naming.memory_index()}/*"],
    })
    return statements


def _knowledge_statements(spec: AgentSpec, ctx: RoleContext) -> list[dict[str, Any]]:
    """Read-only k-NN access to exactly the mounted knowledge bases' indices."""
    if not spec.knowledge_bases or not ctx.memory_domain_arn:
        return []
    return [{
        "Sid": "KnowledgeSearch",
        "Effect": "Allow",
        "Action": ["es:ESHttpGet", "es:ESHttpPost"],
        "Resource": [
            f"{ctx.memory_domain_arn}/{naming.kb_index(kb.kb_id)}/*"
            for kb in spec.knowledge_bases
        ],
    }]
# --- end CN memory/KB (N6) ---


def preset_statements(agent: Agent, spec: AgentSpec, ctx: RoleContext) -> list[dict[str, Any]]:
    """Statements a system preset's role needs beyond the spec-derived policy
    (``SystemPreset.extra_iam`` — the evaluation worker's spans / artefacts / invoke
    grants, N12 plan §B.0); none for ordinary agents."""
    if not getattr(agent, "system_key", None):
        return []
    from app.system_agents.presets import get_preset

    preset = get_preset(agent.system_key or "")
    if preset is None or preset.extra_iam is None:
        return []
    return list(preset.extra_iam(ctx, spec))


def policy_document(
    spec: AgentSpec,
    ctx: RoleContext,
    *,
    extra_statements: list[dict[str, Any]] | None = None,
) -> dict:
    """The capability policy for one agent.

    Statements appear only when the spec calls for them. Sids match the shared CDK
    role so the two can be diffed. ``extra_statements`` are a system preset's additional
    grants (``preset_statements``), appended after the spec-derived ones. Global parity
    (2026-10-27): the policy module never shapes this document — a declared Gateway always
    gets ``AgentCoreGatewayInvoke``; the Gateway interceptor decides each tool call.
    """
    statements: list[dict[str, Any]] = []

    # ---- model credential: every agent fetches the Global Bedrock API key ----
    statements.extend(_model_credential_statements(spec, ctx))

    # ---- CN memory/KB (N6): self-hosted substrate, only when the workspace has it ----
    statements.extend(_memory_statements(spec, ctx))
    statements.extend(_knowledge_statements(spec, ctx))

    # ---- identity / workload tokens ----
    # Tool-level outbound auth: exact per-Connection grants, additive to (and
    # independent of) the family-wide gateway grant below.
    statements.extend(_tool_auth_statements(spec, ctx))
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
                ctx.arn("secretsmanager", "secret:bedrock-agentcore-identity!*")
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
                ctx.arn("bedrock-agentcore", f"gateway/{gateway_id}", region="*")
                for gateway_id in gateway_ids
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
    byoc_kind = spec.byoc.artifact_kind if spec.byoc else None
    if spec.method == "container" or byoc_kind in ("container_source", "container_image"):
        # byoc container_image may name any repo in this account; scope to it
        # rather than the shared launchpad-agents repo
        if byoc_kind == "container_image" and spec.byoc and spec.byoc.image_uri:
            repo_name = spec.byoc.image_uri.split(f".{ctx.dns_suffix}/", 1)[1]
            repo_name = repo_name.split("@", 1)[0].rsplit(":", 1)[0]
            repo_arn = ctx.arn("ecr", f"repository/{repo_name}")
        else:
            repo_arn = ctx.ecr_repo_arn
        statements.append({
            "Sid": "EcrPull",
            "Effect": "Allow",
            "Action": ["ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"],
            "Resource": [repo_arn],
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
                ctx.arn("s3", f"{ctx.artifacts_bucket}/{prefix}*", region="", account="")
                for prefix in prefixes
            ],
        })
        statements.append({
            "Sid": "SkillBundleList",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": [ctx.arn("s3", ctx.artifacts_bucket, region="", account="")],
            "Condition": {"StringLike": {"s3:prefix": [f"{p}*" for p in prefixes]}},
        })

    # ---- routed configuration bundles (config-bundle A/B experiments, N12) ----
    # The generated agent reads its system prompt and tool descriptions from
    # ``_config_bundle()``, which resolves the request's bundle pointer
    # (``launchpad.bundle=<id>:<v>`` — baggage / Custom header / payload alias, CA-2)
    # to ``s3://<artifacts>/bundles/<agent>/<id>/v<n>.json`` **with this role**. The
    # AgentCore ConfigurationBundle service does not exist in aws-cn (plan §B.6), so the
    # grant is a read on exactly that prefix; bundle ids are generated per experiment,
    # so there is no narrower object to name. Unconditional for every Runtime method:
    # the seam is rendered into every template.
    if ctx.artifacts_bucket:
        statements.append({
            "Sid": "LaunchpadBundleRead",
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": [
                ctx.arn("s3", f"{ctx.artifacts_bucket}/bundles/*", region="", account="")
            ],
        })

    # ---- A2A agents legitimately invoke other runtimes ----
    if spec.protocol == "a2a":
        statements.append({
            "Sid": "A2AInvokePeerRuntimes",
            "Effect": "Allow",
            "Action": list(RUNTIME_INVOKE_ACTIONS),
            "Resource": [ctx.arn("bedrock-agentcore", "runtime/*")],
        })
    # ---- A2A *consumers* (N11 r3-C): exactly the mounted peers, nothing wider ----
    # The ARN is the mounted target's own (schema-validated, any partition); the
    # endpoint child resource is what InvokeAgentRuntime with ``qualifier=DEFAULT`` is
    # authorized against alongside the runtime itself.
    peers = _a2a_peer_arns(spec)
    if peers:
        statements.append({
            "Sid": "A2AInvokeMountedPeers",
            "Effect": "Allow",
            "Action": list(RUNTIME_INVOKE_ACTIONS),
            "Resource": [r for arn in peers for r in (arn, f"{arn}/runtime-endpoint/*")],
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
            ctx.arn("logs", "log-group:/aws/bedrock-agentcore/runtimes/*"),
            ctx.arn("logs", "log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"),
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

    statements.extend(extra_statements or [])
    return {"Version": "2012-10-17", "Statement": statements}


# ─── the evaluation worker's execution role (N12 E3-5; plan §B.0) ──────────────────
# Prefixes of the artifacts bucket the worker reads/writes: job artefacts
# (status/results/done + the cancel marker), dataset snapshots it replays, and the
# candidate bundles the optimizer writes.
EVAL_WORKER_PREFIXES: tuple[str, ...] = ("evaluations/*", "eval-datasets/*", "bundles/*")
# N17-obs394: the ONE name prefix of the reviewed code-evaluator Lambdas — the materializer
# (`evaluation_assets.function_name`) names its functions, log groups and roles with it, and the
# worker's grant (and the hub role's `LambdaForCodeEvaluators` in infra/ec2) covers exactly it.
# The grant used to say `launchpad-eval-*`, which matched none of the `launchpad-evalfn-…` names.
CODE_EVALUATOR_FUNCTION_PREFIX = "launchpad-evalfn-"
EVAL_WORKER_CODE_EVALUATOR_FUNCTION = f"function:{CODE_EVALUATOR_FUNCTION_PREFIX}*"


def eval_worker_statements(
    ctx: RoleContext,
    *,
    code_evaluators: bool = False,
    results_mirror_prefix: str = "",
) -> list[dict[str, Any]]:
    """Extra statements of the ``launchpad_eval_worker`` preset's execution role, on top
    of :func:`policy_document` (workload identity + ``ModelCredential*`` already give it
    the Global Bedrock bearer for the judge).

    * ``EvalWorkerSpansRead`` — Logs Insights over ``aws/spans`` and the runtime log
      groups (the spans provider's queries);
    * ``EvalWorkerArtifacts`` / ``EvalWorkerArtifactsList`` — S3 on the three prefixes;
    * ``EvalWorkerInvokeTargets`` — ``InvokeAgentRuntime`` **and**
      ``InvokeAgentRuntimeForUser`` (N12-bug5: the worker's ``runtime_invoke.py`` always
      sends ``runtimeUserId``, which the data plane authorizes as the ForUser action; the
      first action alone was denied on every replay) for replays and rollouts on
      ``runtime/*`` **with ``aws:ResourceAccount`` = this account** (lead ruling 3: runtime
      names are ``sanitize_runtime_name(agent.name)``, so no name-prefix narrowing exists;
      per-job session-policy narrowing is the recorded follow-up R-15). No
      ``StopRuntimeSession``: cancelling a job is the hub's action. An existing worker
      role receives the statement on its next repair/redeploy: ``ensure_role`` re-puts
      the caps inline policy whenever the rendered document differs.
    * optional ``EvalWorkerCodeEvaluators`` (``eval_code_evaluators_enabled``) and
      ``EvalWorkerResultsMirror`` (``eval_results_log_group_prefix``).
    """
    if not ctx.artifacts_bucket:
        raise ValueError("eval_worker_statements needs ctx.artifacts_bucket (run bootstrap)")
    bucket_arn = ctx.arn("s3", ctx.artifacts_bucket, region="", account="")
    statements: list[dict[str, Any]] = [
        {
            "Sid": "EvalWorkerSpansRead",
            "Effect": "Allow",
            "Action": [
                "logs:StartQuery",
                "logs:GetQueryResults",
                "logs:StopQuery",
                "logs:FilterLogEvents",
                "logs:DescribeLogGroups",
            ],
            "Resource": [
                ctx.arn("logs", "log-group:aws/spans"),
                ctx.arn("logs", "log-group:aws/spans:*"),
                ctx.arn("logs", "log-group:/aws/bedrock-agentcore/runtimes/*"),
            ],
        },
        {
            "Sid": "EvalWorkerArtifacts",
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
            "Resource": [f"{bucket_arn}/{prefix}" for prefix in EVAL_WORKER_PREFIXES],
        },
        {
            "Sid": "EvalWorkerArtifactsList",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": [bucket_arn],
            "Condition": {"StringLike": {"s3:prefix": list(EVAL_WORKER_PREFIXES)}},
        },
        {
            "Sid": "EvalWorkerInvokeTargets",
            "Effect": "Allow",
            "Action": list(RUNTIME_INVOKE_ACTIONS),
            "Resource": [ctx.arn("bedrock-agentcore", "runtime/*")],
            "Condition": {"StringEquals": {"aws:ResourceAccount": ctx.account_id}},
        },
    ]
    if code_evaluators:
        statements.append({
            "Sid": "EvalWorkerCodeEvaluators",
            "Effect": "Allow",
            "Action": ["lambda:InvokeFunction"],
            "Resource": [ctx.arn("lambda", EVAL_WORKER_CODE_EVALUATOR_FUNCTION)],
        })
    if results_mirror_prefix:
        prefix = results_mirror_prefix.rstrip("/")
        statements.append({
            "Sid": "EvalWorkerResultsMirror",
            "Effect": "Allow",
            "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": [
                ctx.arn("logs", f"log-group:{prefix}/*"),
                ctx.arn("logs", f"log-group:{prefix}/*:*"),
            ],
        })
    return statements


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
                # N11: only under a non-empty env name, so the default stays byte-identical;
                # scripts/teardown.py sweeps orphans of its own environment by this tag.
                *(
                    [{"Key": naming.ENV_TAG_KEY, "Value": env}]
                    if (env := naming.current_env_name())
                    else []
                ),
            ],
        )
        role_arn = created["Role"]["Arn"]
        log(f"created execution role {name}")
    except Exception as exc:  # noqa: BLE001 — the SDK's typed error is client-specific
        if not _is_already_exists(exc):
            raise
        role = iam.get_role(RoleName=name)["Role"]
        role_arn = role["Arn"]
        # Adopting: refresh the trust policy so a tightened condition still lands — but
        # only when it actually differs. N10v2-bug1 (live, 2026-10-10): a resumed deploy
        # re-entered this path with a byte-identical trust document and the hub's ops
        # role was denied iam:UpdateAssumeRolePolicy; the unconditional rewrite failed
        # the deploy and orphaned the runtime the first attempt had created.
        if _same_policy_document(role.get("AssumeRolePolicyDocument"), trust):
            log(f"reusing existing execution role {name} (trust policy unchanged)")
        else:
            iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust)
            log(f"reusing existing execution role {name} (trust policy refreshed)")

    _put_inline_policy_if_changed(
        iam,
        name,
        capability_policy_name(agent.name),
        policy_document(
            spec, ctx, extra_statements=preset_statements(agent, spec, ctx),
        ),
        log,
    )
    _sync_fs_policy(iam, name, agent, spec, log)
    return role_arn


def _policy_as_dict(value: Any) -> dict | None:
    """Normalize an IAM policy document however a client hands it back: boto3 returns
    `AssumeRolePolicyDocument` / `PolicyDocument` URL-decoded as a dict, the raw API
    (and some stubs) as a URL-encoded or plain JSON string. None when unparseable."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        for text in (value, unquote(value)):
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def _same_policy_document(current: Any, wanted: Any) -> bool:
    cur, want = _policy_as_dict(current), _policy_as_dict(wanted)
    if cur is None or want is None:
        return False
    return json.dumps(cur, sort_keys=True) == json.dumps(want, sort_keys=True)


def _put_inline_policy_if_changed(
    iam: Any, role_name: str, policy_name: str, document: dict, log: Callable[[str], None]
) -> bool:
    """PutRolePolicy only when the stored document differs; a failed read (denied,
    absent, client without GetRolePolicy) means "write it". Returns True on a write."""
    try:
        current = iam.get_role_policy(RoleName=role_name, PolicyName=policy_name)
        current_doc = current.get("PolicyDocument") if isinstance(current, dict) else None
    except Exception:  # noqa: BLE001 — absent policy / denied read / stub without the call
        current_doc = None
    if current_doc is not None and _same_policy_document(current_doc, document):
        log(f"inline policy {policy_name} unchanged")
        return False
    iam.put_role_policy(
        RoleName=role_name, PolicyName=policy_name, PolicyDocument=json.dumps(document)
    )
    return True


def _sync_fs_policy(
    iam: Any, role_name: str, agent: Agent, spec: AgentSpec, log: Callable[[str], None]
) -> None:
    """Attach the BYO-mount policy, or drop a stale one when the mounts were removed
    on re-publish."""
    policy = fs_policy_document(spec)
    name = fs_policy_name(agent.name)
    if policy:
        if _put_inline_policy_if_changed(iam, role_name, name, policy, log):
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
    ctx = role_context(workspace)
    arn = ensure_role(iam, agent, spec, ctx, log)
    name = role_name_for(agent.name, agent.id)
    detail = f"iam role · {name}"
    if fs_policy_document(spec):
        detail += f" (+ {fs_policy_name(agent.name)})"
    return arn, detail


# Outcomes of a role delete (N16-obs76): the teardown report says ``deleted`` only for a
# role that existed — a role never created (deploy failed before provision) or already
# swept is ``absent`` and reported as a skipped step, not a removal.
ROLE_DELETED = "deleted"
ROLE_ABSENT = "absent"
ROLE_FAILED = "failed"


def delete_execution_role(
    agent: Agent,
    settings: Any,
    workspace: WorkspaceContext,
    log: Callable[[str], None] = lambda _m: None,
    iam: Any = None,
) -> bool:
    """Delete the agent's role, if it has one. Never raises."""
    return delete_execution_role_outcome(agent, settings, workspace, log, iam) != ROLE_FAILED


def delete_execution_role_outcome(
    agent: Agent,
    settings: Any,
    workspace: WorkspaceContext,
    log: Callable[[str], None] = lambda _m: None,
    iam: Any = None,
) -> str:
    """``ROLE_DELETED`` / ``ROLE_ABSENT`` / ``ROLE_FAILED`` for the agent's role. Never
    raises. A shared (not per-agent) role is not ours to delete → ``ROLE_ABSENT``."""
    if not settings.per_agent_execution_roles:
        return ROLE_ABSENT  # the shared role is not ours to delete
    if iam is None:
        iam = workspace.client("iam")
    return delete_role_outcome(iam, agent, log)


def delete_role(
    iam: Any, agent: Agent, log: Callable[[str], None] = lambda _m: None
) -> bool:
    """Delete the agent's role and its inline policies. Never raises. ``True`` unless
    the delete failed (an absent role counts as done — see ``delete_role_outcome``)."""
    return delete_role_outcome(iam, agent, log) != ROLE_FAILED


def delete_role_outcome(
    iam: Any, agent: Agent, log: Callable[[str], None] = lambda _m: None
) -> str:
    """Delete the agent's role and its inline policies. Never raises.

    A failed delete must not block deleting the agent, but it must leave the role
    **findable** — hence the log line naming it, and the `launchpad:managed` tag that
    lets `scripts/teardown.py` sweep orphans. Returns ``ROLE_DELETED``, ``ROLE_ABSENT``
    (no such role — never created, or already gone) or ``ROLE_FAILED``.
    """
    name = role_name_for(agent.name, agent.id)
    try:
        listed = iam.list_role_policies(RoleName=name).get("PolicyNames", [])
    except Exception as exc:  # noqa: BLE001
        if _is_no_such_entity(exc):
            return ROLE_ABSENT  # already gone
        # N10v2-bug8 (live): the hub's ops role may be denied iam:ListRolePolicies; the
        # platform attaches exactly two inline policies with deterministic names, so the
        # denial falls back to those instead of letting DeleteRole hit DeleteConflict.
        listed = [capability_policy_name(agent.name), fs_policy_name(agent.name)]
        log(
            f"could not list policies on {name}: {exc} — falling back to the platform's "
            f"known policy names {listed}"
        )
    for policy_name in listed:
        try:
            iam.delete_role_policy(RoleName=name, PolicyName=policy_name)
        except Exception as exc:  # noqa: BLE001
            if _is_no_such_entity(exc):
                continue  # a known name that was never attached (no BYO mounts)
            log(f"could not delete inline policy {policy_name} on {name}: {exc}")
    try:
        iam.delete_role(RoleName=name)
        log(f"deleted execution role {name}")
        return ROLE_DELETED
    except Exception as exc:  # noqa: BLE001
        if _is_no_such_entity(exc):
            return ROLE_ABSENT
        log(
            f"could not delete execution role {name}: {exc} — it is tagged "
            f"{MANAGED_TAG_KEY}={agent.id} and can be swept by scripts/teardown.py"
        )
        return ROLE_FAILED


# Every grant that lets a workload call InvokeAgentRuntime carries BOTH actions
# (N12-bug5, live t1): the data plane authorizes ``InvokeAgentRuntimeForUser`` as soon
# as the call carries ``runtimeUserId`` — which every Launchpad caller does (the hub's
# ``services/invoke.py``, the evaluation worker's ``strands_eval/runtime_invoke.py``,
# the Skill Lab runner), because the Runtime only injects a workload access token for
# a user-scoped invoke and the templates self-mint otherwise (N10v3-bug5). The A2A
# consumer template does not pass it yet; its grant carries the action so the fix for
# that template needs no role change. ``InvokeAgentRuntime`` alone answered
# ``AccessDeniedException … not authorized to perform:
# bedrock-agentcore:InvokeAgentRuntimeForUser`` on every worker replay.
RUNTIME_INVOKE_ACTIONS: tuple[str, ...] = (
    "bedrock-agentcore:InvokeAgentRuntime",
    "bedrock-agentcore:InvokeAgentRuntimeForUser",
)


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
#   ValidationException: Role validation failed for 'arn:<partition>:iam::…:role/…'. Please
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
