"""The COA context layer: the workspace's connected Context Ontology Accelerator.

Launchpad **connects to** a COA deployment and never deploys, changes or tears one
down. An admin names it by stack prefix, env and region (``PUT /api/context-layer``);
everything else is discovered from the deployment itself:

- SSM ``/<prefix>/{mcp/runtime-arn, serve/runtime-arn, userpool-id, issuer,
  agent-oauth-client-id}`` — the last exists only when COA's agent OAuth client
  (3LO) is enabled, which is the only client a platform can act for a user with;
- the CloudFormation outputs of ``<prefix>-<env>-{api,web,auth}`` (REST endpoint,
  console URL, Cognito hosted-UI domain);
- ``GetAgentRuntime`` on both runtimes, which must be READY.

Every discovery read targets the **COA** region (``replace(workspace, region=…)``),
which may differ from the workspace's; every token-vault call stays in the
workspace region, next to the workload identities that use the Connection.

Connecting creates (or adopts) one OAuth2 Connection holding the COA agent client,
and ensures the console workload identity ``launchpad-console-<workspace>`` the
module and Harness agents fetch members' COA tokens with
(``context_layer_tokens``). The client secret is read once, during connect, and
handed straight to the vault: it is never stored, logged or returned.

The ``context_layers`` row stores identifiers only. Runtime status, the callback
registration and whether agent OAuth is still on are read live by ``status``.
"""

import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.orm import Session

from app.core.errors import AppError, aws_error_code
from app.models.ledger import Agent
from app.models.ledger import ContextLayer as ContextLayerRow
from app.schemas.agent import AgentSpec, ToolRef
from app.services import identity_providers, oauth_sessions
from app.services.agentcore.client import control_client
from app.services.creators import Creator
from app.services.workspace import WorkspaceContext
from app.templates import context_layer_support as support

logger = logging.getLogger("launchpad.context_layer")

PREFIX_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
ENV_RE = re.compile(r"^[a-z][a-z0-9-]{0,15}$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d{1,2}$")
CONSOLE_WORKLOAD_PREFIX = "launchpad-console-"

# resources key → SSM parameter suffix under /<prefix>/
SSM_KEYS = {
    "mcp_runtime_arn": "mcp/runtime-arn",
    "serve_runtime_arn": "serve/runtime-arn",
    "user_pool_id": "userpool-id",
    "issuer": "issuer",
    "agent_client_id": "agent-oauth-client-id",
}
AGENT_CLIENT_KEY = "agent_client_id"
RUNTIME_KEYS = {"mcp": "mcp_runtime_arn", "serve": "serve_runtime_arn"}
_RUNTIME_ARN_RE = re.compile(
    r"^arn:aws[\w-]*:bedrock-agentcore:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):"
    r"runtime/(?P<id>[A-Za-z0-9_-]+)$"
)


@dataclass(frozen=True)
class ContextLayer:
    """A connected layer: the ledger row plus the URLs derived from it."""

    id: str
    workspace_id: str
    prefix: str
    env: str
    coa_region: str
    resources: dict[str, Any]
    connection: str
    console_workload: str
    created_by_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def mcp_url(self) -> str:
        return support.invocations_url(self.coa_region, self.resources["mcp_runtime_arn"])

    @property
    def serve_url(self) -> str:
        return support.invocations_url(self.coa_region, self.resources["serve_runtime_arn"])

    @property
    def api_endpoint(self) -> str:
        return str(self.resources.get("api_endpoint") or "")

    @property
    def console_url(self) -> str:
        return str(self.resources.get("console_url") or "")

    @property
    def name(self) -> str:
        """How the deployment is labelled: its stack family ``<prefix>-<env>``."""
        return f"{self.prefix}-{self.env}"


def _layer(row: ContextLayerRow) -> ContextLayer:
    return ContextLayer(
        id=row.id,
        workspace_id=row.workspace_id or "",
        prefix=row.prefix,
        env=row.env,
        coa_region=row.coa_region,
        resources=dict(row.resources or {}),
        connection=row.connection,
        console_workload=row.console_workload,
        created_by_name=row.created_by_name,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def not_connected() -> AppError:
    return AppError(
        "context_layer.not_connected",
        "this workspace has no connected COA context layer — an administrator "
        "connects one on the Context Layer page",
        status_code=409,
    )


def _row(db: Session, workspace_id: str) -> ContextLayerRow | None:
    return (
        db.query(ContextLayerRow).filter(ContextLayerRow.workspace_id == workspace_id).first()
    )


def get(db: Session, workspace_id: str) -> ContextLayer | None:
    row = _row(db, workspace_id)
    return _layer(row) if row is not None else None


def require(db: Session, workspace_id: str) -> ContextLayer:
    layer = get(db, workspace_id)
    if layer is None:
        raise not_connected()
    return layer


def require_connected(workspace_id: str) -> ContextLayer:
    """``require`` through its own short session (request validation, deploy stages)."""
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        return require(db, workspace_id)
    finally:
        db.close()


def connection_for(db: Session, workspace_id: str) -> str | None:
    """The layer's Connection name, or None when nothing is connected."""
    row = _row(db, workspace_id)
    return row.connection if row is not None else None


def console_workload_name(workspace_id: str) -> str:
    """The console workload identity of one workspace (``[A-Za-z0-9_.-]``, ≤ 255)."""
    slug = re.sub(r"[^A-Za-z0-9_.-]", "-", workspace_id)[:200] or "default"
    return f"{CONSOLE_WORKLOAD_PREFIX}{slug}"


def default_connection_name(prefix: str) -> str:
    return f"coa-{prefix}"[:128]


def coa_context(workspace: WorkspaceContext, region: str) -> WorkspaceContext:
    """The same account and credentials, aimed at the COA deployment's region."""
    return workspace if region == workspace.region else replace(workspace, region=region)


def _aws_refusal(what: str, exc: Exception) -> AppError:
    code = aws_error_code(exc) if isinstance(exc, ClientError) else type(exc).__name__
    return AppError(
        "context_layer.discovery_failed",
        f"could not read the COA deployment's {what} ({code})",
        {"source": what, "code": code},
        status_code=502,
    )


# ── discovery ────────────────────────────────────────────────────────────────


def _validate_inputs(prefix: str, env: str, region: str) -> None:
    bad = [
        name for name, value, pattern in (
            ("prefix", prefix, PREFIX_RE), ("env", env, ENV_RE), ("region", region, REGION_RE),
        )
        if not pattern.fullmatch(value or "")
    ]
    if bad:
        raise AppError(
            "context_layer.invalid_request",
            f"invalid {', '.join(bad)} for a COA deployment",
            {"fields": bad},
            status_code=422,
        )


def _ssm_values(ctx: WorkspaceContext, prefix: str) -> tuple[dict[str, str], dict[str, str]]:
    names = {key: f"/{prefix}/{suffix}" for key, suffix in SSM_KEYS.items()}
    try:
        found = ctx.client("ssm").get_parameters(Names=list(names.values())).get("Parameters")
    except (ClientError, BotoCoreError) as exc:
        raise _aws_refusal("SSM parameters", exc) from exc
    values = {str(p.get("Name")): str(p.get("Value") or "") for p in found or []}
    return {key: values.get(name, "") for key, name in names.items()}, names


def _stack_outputs(cfn: Any, stack: str) -> dict[str, str]:
    try:
        stacks = cfn.describe_stacks(StackName=stack).get("Stacks") or []
    except ClientError as exc:
        if aws_error_code(exc) == "ValidationError":  # "Stack with id … does not exist"
            raise AppError(
                "context_layer.not_found",
                f"no CloudFormation stack {stack} in the COA region",
                {"stack": stack},
                status_code=404,
            ) from exc
        raise _aws_refusal(f"stack {stack}", exc) from exc
    except BotoCoreError as exc:
        raise _aws_refusal(f"stack {stack}", exc) from exc
    outputs = (stacks[0].get("Outputs") if stacks else None) or []
    return {str(o.get("OutputKey")): str(o.get("OutputValue") or "") for o in outputs}


def _output(outputs: dict[str, str], key: str, stack: str) -> str:
    """An output by key. CDK prefixes construct outputs with their path and a hash
    (``PublicUIWebsiteURL0A1B…``), so a key that merely contains ``key`` counts."""
    value = outputs.get(key) or next(
        (v for k, v in sorted(outputs.items()) if key.lower() in k.lower() and v), ""
    )
    if not value:
        raise AppError(
            "context_layer.not_found",
            f"stack {stack} has no {key} output",
            {"stack": stack, "output": key},
            status_code=404,
        )
    return value


def _https_url(value: str, label: str) -> str:
    """A stack-output URL Launchpad will send members' tokens to or link to:
    https, a real host, no credentials or fragment. A custom domain is accepted
    because it IS the stack output, read live, never a caller's input."""
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.fragment:
        raise AppError(
            "context_layer.discovery_failed",
            f"the COA {label} is not an https URL",
            {"source": label},
            status_code=502,
        )
    return value.strip()


def _runtime_parts(arn: str) -> dict[str, str]:
    match = _RUNTIME_ARN_RE.fullmatch(arn or "")
    if match is None:
        raise AppError(
            "context_layer.discovery_failed",
            "a COA runtime ARN in SSM is not an AgentCore Runtime ARN",
            {"source": "runtime-arn"},
            status_code=502,
        )
    return match.groupdict()


def runtime_status(ctx: WorkspaceContext, arn: str) -> str:
    """``GetAgentRuntime`` status (COA region). Only the status is read back — the
    response also carries the runtime's environment variables."""
    runtime = control_client(ctx).get_agent_runtime(agentRuntimeId=_runtime_parts(arn)["id"])
    return str(runtime.get("status") or "")


def discover(
    workspace: WorkspaceContext, *, prefix: str, env: str, region: str
) -> dict[str, str]:
    """Resolve a COA deployment into the ``resources`` identifiers, or refuse by name.

    Refusals: the parameters are missing (``context_layer.not_found``), agent OAuth is
    off (``context_layer.agent_oauth_disabled``), the deployment is in another account
    (``context_layer.cross_account``), a runtime is not READY
    (``context_layer.runtime_not_ready``).
    """
    _validate_inputs(prefix, env, region)
    ctx = coa_context(workspace, region)
    resources, names = _ssm_values(ctx, prefix)
    missing = [names[k] for k, v in resources.items() if not v and k != AGENT_CLIENT_KEY]
    if missing:
        raise AppError(
            "context_layer.not_found",
            f"no COA deployment with prefix {prefix!r} in {region}: SSM parameters missing",
            {"missing": missing},
            status_code=404,
        )
    if not resources[AGENT_CLIENT_KEY]:
        raise AppError(
            "context_layer.agent_oauth_disabled",
            "the COA deployment has no agent OAuth client — deploy its auth, serve and mcp "
            "stacks with the agentOauthCallbackUrls context first",
            {"parameter": names[AGENT_CLIENT_KEY]},
            status_code=409,
        )
    runtimes = {kind: _runtime_parts(resources[key]) for kind, key in RUNTIME_KEYS.items()}
    accounts = {parts["account"] for parts in runtimes.values()}
    if accounts != {workspace.account_id}:
        raise AppError(
            "context_layer.cross_account",
            "the COA deployment must be in this workspace's AWS account",
            {"workspace_account": workspace.account_id, "coa_accounts": sorted(accounts)},
            status_code=422,
        )
    if {parts["region"] for parts in runtimes.values()} != {region}:
        raise AppError(
            "context_layer.discovery_failed",
            f"the COA runtimes are not in {region}",
            {"source": "runtime-arn"},
            status_code=502,
        )
    issuer = resources["issuer"].rstrip("/")
    if issuer != f"https://cognito-idp.{region}.amazonaws.com/{resources['user_pool_id']}":
        raise AppError(
            "context_layer.discovery_failed",
            "the COA issuer is not its Cognito user pool",
            {"source": "issuer"},
            status_code=502,
        )
    resources["issuer"] = issuer
    cfn = ctx.client("cloudformation")
    stacks = {suffix: f"{prefix}-{env}-{suffix}" for suffix in ("api", "web", "auth")}
    outputs = {suffix: _stack_outputs(cfn, stack) for suffix, stack in stacks.items()}
    resources["api_endpoint"] = _https_url(
        _output(outputs["api"], "ApiEndpoint", stacks["api"]), "REST endpoint"
    )
    resources["console_url"] = _https_url(
        _output(outputs["web"], "WebsiteURL", stacks["web"]), "console URL"
    )
    resources["hosted_ui_domain"] = _https_url(
        _output(outputs["auth"], "CognitoDomainUrl", stacks["auth"]), "hosted UI domain"
    )
    for kind, key in RUNTIME_KEYS.items():
        try:
            status = runtime_status(ctx, resources[key])
        except (ClientError, BotoCoreError) as exc:
            raise _aws_refusal(f"{kind} runtime", exc) from exc
        if status != "READY":
            raise AppError(
                "context_layer.runtime_not_ready",
                f"the COA {kind} runtime is {status or 'unknown'}, not READY",
                {"runtime": kind, "status": status},
                status_code=409,
            )
    return resources


def _agent_client(ctx: WorkspaceContext, resources: dict[str, Any]) -> dict[str, Any]:
    """The COA agent OAuth client (``DescribeUserPoolClient``, COA region). The
    response carries the client secret; callers use it once and drop it."""
    try:
        return ctx.client("cognito-idp").describe_user_pool_client(
            UserPoolId=resources["user_pool_id"], ClientId=resources[AGENT_CLIENT_KEY]
        ).get("UserPoolClient") or {}
    except (ClientError, BotoCoreError) as exc:
        raise _aws_refusal("agent OAuth client", exc) from exc


def _provider_client_id(provider: dict[str, Any]) -> str:
    output = provider.get("oauth2ProviderConfigOutput") or {}
    return str((output.get("customOauth2ProviderConfig") or {}).get("clientId") or "")


# ── connect / disconnect ─────────────────────────────────────────────────────


def ensure_console_workload(control: Any, name: str, return_url: str) -> bool:
    """Create the console workload identity, or add the return URL to its
    allow-list (other entries are kept). True when anything was written."""
    try:
        identity = control.get_workload_identity(name=name)
    except ClientError as exc:
        if aws_error_code(exc) != "ResourceNotFoundException":
            raise
        control.create_workload_identity(name=name, allowedResourceOauth2ReturnUrls=[return_url])
        return True
    current = list(identity.get("allowedResourceOauth2ReturnUrls") or [])
    if return_url in current:
        return False
    control.update_workload_identity(
        name=name, allowedResourceOauth2ReturnUrls=sorted({*current, return_url})
    )
    return True


def _connection(
    control: Any,
    db: Session,
    workspace: WorkspaceContext,
    resources: dict[str, Any],
    client: dict[str, Any],
    *,
    name: str,
    adopt_only: bool,
    prefix: str,
    env: str,
    creator: Creator | None,
) -> str:
    """Adopt ``name`` when it exists (its client id must be the COA agent client),
    else create it. Returns the Connection's callback URL ("" when unknown)."""
    try:
        provider = identity_providers.live_provider(control, identity_providers.KIND_OAUTH2, name)
    except AppError as exc:
        if exc.code != "identity.connection_not_found":
            raise
        provider = None
    if provider is not None:
        client_id = _provider_client_id(provider) or identity_providers.ledger_client_id(
            db, workspace.id, name
        ) or ""
        if client_id != resources[AGENT_CLIENT_KEY]:
            raise AppError(
                "context_layer.connection_mismatch",
                f"Connection {name!r} does not hold the COA agent OAuth client",
                {"connection": name},
                status_code=422,
            )
        return str(provider.get("callbackUrl") or "")
    if adopt_only:
        raise AppError(
            "context_layer.connection_not_found",
            f"no OAuth2 Connection named {name!r} in this workspace",
            {"connection": name},
            status_code=404,
        )
    secret = str(client.get("ClientSecret") or "")
    if not secret:
        raise AppError(
            "context_layer.discovery_failed",
            "the COA agent OAuth client has no client secret (it must be confidential)",
            {"source": "agent OAuth client"},
            status_code=502,
        )
    created = identity_providers.create_oauth2_connection(
        control,
        db,
        workspace.id,
        name=name,
        vendor="CustomOauth2",
        client_id=resources[AGENT_CLIENT_KEY],
        client_secret=secret,
        discovery_url=f"{resources['issuer']}/.well-known/openid-configuration",
        scopes=list(support.SCOPES),
        template="cognito",
        description=f"COA context layer {prefix}-{env}",
        created_by=(creator.name if creator else "") or "",
    )
    return str(created.get("callback_url") or "")


def connect(
    db: Session,
    workspace: WorkspaceContext,
    *,
    prefix: str,
    env: str,
    region: str,
    connection: str | None,
    return_url: str,
    creator: Creator | None = None,
) -> ContextLayer:
    """Discover, then create/adopt the Connection, ensure the console workload
    identity and upsert the row. Re-connecting updates the row in place."""
    resources = discover(workspace, prefix=prefix, env=env, region=region)
    client = _agent_client(coa_context(workspace, region), resources)
    control = control_client(workspace)
    name = connection or default_connection_name(prefix)
    _connection(
        control, db, workspace, resources, client, name=name, adopt_only=bool(connection),
        prefix=prefix, env=env, creator=creator,
    )
    client.clear()  # the secret goes no further than the vault call above
    workload = console_workload_name(workspace.id)
    try:
        ensure_console_workload(control, workload, return_url)
    except (ClientError, BotoCoreError) as exc:
        code = aws_error_code(exc) if isinstance(exc, ClientError) else type(exc).__name__
        raise AppError(
            "context_layer.identity_failed",
            f"could not set up the console workload identity {workload} ({code})",
            {"workload": workload, "code": code},
            status_code=502,
        ) from exc
    row = _row(db, workspace.id)
    if row is None:
        row = ContextLayerRow(workspace_id=workspace.id)
        if creator is not None:
            row.created_by, row.created_by_name = creator.user_id, creator.name
        db.add(row)
    row.prefix, row.env, row.coa_region = prefix, env, region
    row.resources = resources
    row.connection = name
    row.console_workload = workload
    row.updated_at = datetime.now(UTC)
    db.commit()
    from app.services import context_layer_tokens

    context_layer_tokens.forget(workspace.id)  # a new deployment: no cached token carries over
    forget_callback_check(workspace.id)  # (re)connecting is how an admin re-checks it
    logger.info(
        "workspace %s connected to COA %s-%s (%s) via connection %s",
        workspace.id, prefix, env, region, name,
    )
    return _layer(row)


def referencing_agents(db: Session, workspace_id: str) -> list[dict[str, str]]:
    """Live agents whose stored spec attaches the context layer."""
    rows = (
        db.query(Agent)
        .filter(Agent.workspace_id == workspace_id, Agent.status != "deleted")
        .all()
    )
    return [
        {"type": "agent", "id": agent.id, "name": agent.name}
        for agent in rows
        if support.attached(agent.spec)
    ]


def disconnect(db: Session, workspace: WorkspaceContext) -> dict[str, Any]:
    """Forget the layer; refused (409 ``context_layer.referenced``) while any agent
    attaches it. The Connection and the console workload identity stay: they are
    listed under 出站凭证 and swept by teardown."""
    row = _row(db, workspace.id)
    if row is None:
        raise not_connected()
    refs = referencing_agents(db, workspace.id)
    if refs:
        raise AppError(
            "context_layer.referenced",
            f"{len(refs)} agent(s) attach the context layer; detach it from them first",
            {"referenced_by": refs},
            status_code=409,
        )
    out = {
        "disconnected": True,
        "connection": row.connection,
        "console_workload": row.console_workload,
    }
    db.delete(row)
    db.commit()
    from app.services import context_layer_tokens

    context_layer_tokens.forget(workspace.id)
    return out


# ── status ───────────────────────────────────────────────────────────────────


def my_authorization(db: Session, workspace_id: str, username: str, connection: str) -> dict:
    """The member's console grant: ``none | pending | authorized | revoked``."""
    state = oauth_sessions.grant_status(
        db, workspace_id, user_id=username, provider=connection, agent_id=""
    )
    status = state["status"]
    if status == "authorized" and state.get("force_reauth"):
        status = "revoked"
    return {"status": status, "authorized_at": state.get("authorized_at")}


def _probe(errors: list[dict[str, str]], what: str, call: Any) -> Any:
    try:
        return call()
    except (ClientError, BotoCoreError, AppError) as exc:
        code = (
            aws_error_code(exc) if isinstance(exc, ClientError)
            else getattr(exc, "code", None) or type(exc).__name__
        )
        errors.append({"probe": what, "code": str(code)})
        return None


# (workspace id, callback URL) -> (monotonic expiry, registered)
_CALLBACK_CACHE: dict[tuple[str, str], tuple[float, bool]] = {}
CALLBACK_CACHE_TTL_S = 300


def _callback_registered(
    errors: list[dict[str, str]], ctx: WorkspaceContext, layer: ContextLayer,
    callback_url: str | None,
) -> bool | None:
    """Whether the Connection's callback is on the COA agent client (None when
    unknown). Cached per workspace + callback; a failed read is not cached."""
    if not callback_url:
        return None
    key = (layer.workspace_id, callback_url)
    hit = _CALLBACK_CACHE.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    client = _probe(errors, "agent_client", lambda: _agent_client(ctx, layer.resources))
    if client is None:
        return None
    registered = callback_url in list(client.get("CallbackURLs") or [])
    client.clear()
    _CALLBACK_CACHE[key] = (time.monotonic() + CALLBACK_CACHE_TTL_S, registered)
    return registered


def forget_callback_check(workspace_id: str) -> None:
    for key in [key for key in _CALLBACK_CACHE if key[0] == workspace_id]:
        _CALLBACK_CACHE.pop(key, None)


def status(
    db: Session, workspace: WorkspaceContext, *, username: str, is_admin: bool
) -> dict[str, Any]:
    """``GET /api/context-layer``: the deployment summary, its live health and the
    caller's own authorization. No secret, ever; the callback URL and the agent
    client id (what an operator registers in COA) only for administrators."""
    layer = get(db, workspace.id)
    if layer is None:
        return {"connected": False, "can_manage": is_admin}
    errors: list[dict[str, str]] = []
    ctx = coa_context(workspace, layer.coa_region)
    runtimes = {
        kind: _probe(
            errors, f"{kind}_runtime", lambda k=key: runtime_status(ctx, layer.resources[k])
        )
        for kind, key in RUNTIME_KEYS.items()
    }

    def _agent_oauth_on() -> bool:
        values, _ = _ssm_values(ctx, layer.prefix)
        return bool(values.get(AGENT_CLIENT_KEY))

    agent_oauth = _probe(errors, "agent_oauth", _agent_oauth_on)
    provider = _probe(
        errors,
        "connection",
        lambda: identity_providers.live_provider(
            control_client(workspace), identity_providers.KIND_OAUTH2, layer.connection
        ),
    )
    callback_url = str((provider or {}).get("callbackUrl") or "") or None
    # Only an administrator can act on it (it is fixed in COA's deploy), and the
    # read returns the client secret — so members never trigger it, and the
    # answer is reused for a few minutes instead of re-read on every page load.
    callback_registered = (
        _callback_registered(errors, ctx, layer, callback_url) if is_admin else None
    )
    out: dict[str, Any] = {
        "connected": True,
        "can_manage": is_admin,
        "name": layer.name,
        "prefix": layer.prefix,
        "env": layer.env,
        "region": layer.coa_region,
        "console_url": layer.console_url,
        "api_endpoint": layer.api_endpoint,
        "issuer": layer.resources.get("issuer"),
        "mcp_runtime_arn": layer.resources.get("mcp_runtime_arn"),
        "serve_runtime_arn": layer.resources.get("serve_runtime_arn"),
        "connection": layer.connection,
        "console_workload": layer.console_workload,
        "runtimes": runtimes,
        "ready": all(value == "READY" for value in runtimes.values()),
        "agent_oauth_enabled": agent_oauth,
        "callback_registered": callback_registered,
        "authorization": my_authorization(db, workspace.id, username, layer.connection),
        "referenced_by": referencing_agents(db, workspace.id),
        "created_by_name": layer.created_by_name,
        "created_at": layer.created_at.isoformat() if layer.created_at else None,
        "updated_at": layer.updated_at.isoformat() if layer.updated_at else None,
        "probe_errors": errors,
    }
    if is_admin:
        out["callback_url"] = callback_url
        out["agent_client_id"] = layer.resources.get(AGENT_CLIENT_KEY)
        out["user_pool_id"] = layer.resources.get("user_pool_id")
        # the CDK context COA's deploy must carry so the callback stays registered
        out["callback_context_key"] = "agentOauthCallbackUrls"
    return out


# ── agent specs ──────────────────────────────────────────────────────────────


def grant_agent_id(db: Session, workspace_id: str, agent_id: str, provider: str) -> str:
    """The ``user_grants.agent_id`` a Chat auth card polls for.

    A Harness agent's COA consent is asked on the console workload identity, so its
    grant is the console grant (``agent_id=""``), not one per agent; every other
    pair is what it says."""
    if not agent_id:
        return agent_id
    agent = db.get(Agent, agent_id)
    if (
        agent is not None
        and agent.workspace_id == workspace_id
        and agent.method == "harness"
        and support.attached(agent.spec)
        and connection_for(db, workspace_id) == provider
    ):
        return ""
    return agent_id


def expanded_spec(spec: AgentSpec, layer: ContextLayer) -> AgentSpec:
    """The deploy-time spec of a Strands agent: ``context_layer`` becomes one
    ``as_user`` MCP tool on the layer's Connection plus the prompt addendum. The
    single expansion every deploy consumer (template, IAM, environment, return-URL
    allow-list) reads. Never stored: the agent row keeps only ``context_layer``."""
    if spec.context_layer is None:
        return spec
    tool = ToolRef(**support.strands_tool_ref(layer.mcp_url, layer.connection))
    return spec.model_copy(update={
        "tools": [*spec.tools, tool],
        "system_prompt": spec.system_prompt + support.addendum_for(spec, tool_prefix=""),
    })


def deploy_spec(spec_dict: dict[str, Any], workspace_id: str) -> AgentSpec:
    """``AgentSpec(**stored)``, expanded when it attaches the context layer (zip
    deploy stages). A layer gone since save fails the stage by name."""
    spec = AgentSpec(**spec_dict)
    if spec.context_layer is None:
        return spec
    return expanded_spec(spec, require_connected(workspace_id))
