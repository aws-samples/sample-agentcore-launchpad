"""Inbound-auth service: workspace default storage, discovery probing, and the
agent-facing resolution that folds spec + workspace default together.

The pure models and precedence rule live in ``app.schemas.inbound_auth``; this
module adds the two impure pieces — the ledger (workspace ``settings`` blob) and
the one-shot GET of the discovery document — plus the token side of invoking a
JWT-mode agent (Cognito client_credentials for non-interactive callers).
"""

import base64
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.ledger import Agent, Workspace
from app.schemas.inbound_auth import (
    InboundAuth,
    authorizer_configuration,
    issuer_from_discovery_url,
    parse_inbound_auth,
    resolve_inbound_auth,
)

__all__ = [
    "InboundAuth",
    "authorizer_configuration",
    "deployed_inbound_auth",
    "display_mode",
    "is_jwt_mode",
    "issuer_mismatch",
    "m2m_bearer_token",
    "probe_discovery",
    "record_deployed_auth",
    "require_platform_reachable",
    "resolve_for_agent",
    "set_workspace_default",
    "suggested_cognito_default",
    "validate_inbound_auth",
    "workspace_cognito_issuer",
    "workspace_default",
]
from app.services.workspace import WorkspaceContext, get_workspace_row

logger = logging.getLogger("launchpad.inbound_auth")

SETTINGS_KEY = "inbound_auth_default"
DISCOVERY_PROBE_TIMEOUT_S = 5.0

# GET of a public .well-known document; nothing secret goes out or gets cached.
ProbeFn = Callable[[str], dict[str, Any]]


def _default_probe(url: str) -> dict[str, Any]:
    response = httpx.get(url, timeout=DISCOVERY_PROBE_TIMEOUT_S, follow_redirects=True)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("discovery document is not a JSON object")
    return body


def probe_discovery(url: str, probe: ProbeFn | None = None) -> dict[str, Any]:
    """Fetch + sanity-check the OIDC discovery document once, at save time.

    A wrong URL otherwise surfaces only as opaque 403s on the first invoke;
    failing the save with the real reason is the cheapest place to catch it.
    Returns the (non-secret) fields the UI echoes back: issuer + token endpoint.
    """
    try:
        document = (probe or _default_probe)(url)
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            "identity.discovery_unreachable",
            f"could not fetch the OIDC discovery document at {url}: "
            f"{type(exc).__name__}: {exc}",
            status_code=422,
        ) from exc
    if not str(document.get("jwks_uri") or ""):
        raise AppError(
            "identity.discovery_invalid",
            f"the document at {url} has no jwks_uri — it is not an OIDC "
            "discovery document, so the Runtime authorizer could not validate "
            "any token signature",
            status_code=422,
        )
    return {
        "issuer": str(document.get("issuer") or ""),
        "token_endpoint": str(document.get("token_endpoint") or ""),
        "jwks_uri": str(document.get("jwks_uri") or ""),
    }


def validate_inbound_auth(auth: InboundAuth, probe: ProbeFn | None = None) -> None:
    """Request-time validation beyond the pydantic shape: the discovery probe."""
    if auth.mode == "jwt" and auth.jwt is not None:
        probe_discovery(auth.jwt.discovery_url, probe)


# ── workspace default ────────────────────────────────────────────────────────


def workspace_default(row: Workspace | None) -> InboundAuth | None:
    """The workspace's stored inbound-auth default, or None (= IAM)."""
    if row is None:
        return None
    return parse_inbound_auth((row.settings or {}).get(SETTINGS_KEY))


def set_workspace_default(db: Session, row: Workspace, auth: InboundAuth) -> None:
    """Persist the default onto the workspace settings blob.

    Reassigned, not mutated — SQLAlchemy does not track in-place JSON edits.
    Deliberately does NOT touch deployed agents: an existing Runtime keeps the
    authorizer it was deployed with until its next redeploy resolves afresh.
    """
    row.settings = {**(row.settings or {}), SETTINGS_KEY: auth.model_dump()}
    db.commit()


def resolve_for_agent(agent: Agent, db: Session) -> InboundAuth:
    """The effective inbound auth this agent's next deploy will apply.

    Reads via getattr so a lightweight stand-in (provision-stage IAM tests pass
    a SimpleNamespace) resolves like a row with those fields unset."""
    spec = getattr(agent, "spec", None) or {}
    workspace_id = getattr(agent, "workspace_id", None)
    row = get_workspace_row(db, workspace_id) if workspace_id else None
    return resolve_inbound_auth(
        parse_inbound_auth(spec.get("inbound_auth")),
        workspace_default(row),
        method=str(getattr(agent, "method", "") or ""),
        protocol=str(spec.get("protocol") or "http"),
    )


def record_deployed_auth(agent: Agent, resolved: InboundAuth) -> None:
    """Snapshot the resolved choice onto the row (caller commits).

    Written by the deploy stage in the same commit as the runtime identifiers,
    so the ledger's answer to "how do I call this agent" always matches the
    runtime that answered the Create/Update call.
    """
    agent.inbound_auth_mode = resolved.mode
    agent.inbound_auth_config = (
        resolved.model_dump() if resolved.mode == "jwt" else None
    )


def deployed_inbound_auth(agent: Agent) -> InboundAuth:
    """The inbound auth the LIVE Runtime carries — the deploy-time snapshot.

    NULL mode (rows predating the feature, and non-Runtime methods) reads back
    as IAM: every such runtime was created without an authorizer.
    """
    if agent.inbound_auth_mode == "jwt":
        parsed = parse_inbound_auth(agent.inbound_auth_config)
        if parsed is not None:
            return parsed
        # A jwt-mode row whose snapshot cannot be parsed is a bug, not IAM —
        # treating it as IAM would sign requests the runtime then 403s. The
        # invoke path keys off ``.mode`` only, so a config-less jwt marker
        # (bypassing the mode=jwt-requires-config validator) keeps it correct.
        logger.warning(
            "agent %s has inbound_auth_mode=jwt but an unreadable config snapshot",
            agent.id,
        )
        return InboundAuth.model_construct(mode="jwt", jwt=None)
    return InboundAuth(mode="iam")


def is_jwt_mode(agent: Agent) -> bool:
    return agent.inbound_auth_mode == "jwt"


def display_mode(agent: Agent) -> str:
    """The mode badge the console shows.

    Launchpad-deployed agents read the deploy snapshot; a discovered import has
    no snapshot, but its scan recorded ``authorizer_type`` — surface an external
    custom JWT authorizer in the same field so imported agents show their mode.
    """
    if agent.inbound_auth_mode:
        return agent.inbound_auth_mode
    discovery = (agent.spec or {}).get("discovery") or {}
    if str(discovery.get("authorizer_type") or "").lower() == "custom_jwt":
        return "jwt"
    return "iam"


def workspace_cognito_issuer(workspace: WorkspaceContext) -> str | None:
    """The issuer of every token the platform itself presents, or None.

    Console Chat ("invoke as me" and the M2M path), ``/v1``, direct invoke and
    evaluation all send a token of the workspace Cognito pool
    (``cognito-idp.<region>.amazonaws.com/<pool>``), so a JWT authorizer on any
    other issuer refuses every one of them.
    """
    pool_id = str(workspace.resources.get("user_pool_id") or "")
    if not pool_id:
        return None
    return f"https://cognito-idp.{workspace.region}.amazonaws.com/{pool_id}"


def issuer_mismatch(auth: InboundAuth, workspace: WorkspaceContext) -> dict[str, str] | None:
    """``{agent_issuer, workspace_issuer}`` when a JWT authorizer trusts an
    issuer other than the workspace pool, else None (IAM, no pool, or unknown).

    Compared on the discovery URL prefix — the issuer by OIDC Discovery §4 — so
    no network read is needed on the invoke path.
    """
    if auth.mode != "jwt" or auth.jwt is None:
        return None
    workspace_issuer = workspace_cognito_issuer(workspace)
    agent_issuer = issuer_from_discovery_url(auth.jwt.discovery_url)
    if not (workspace_issuer and agent_issuer) or agent_issuer == workspace_issuer:
        return None
    return {"agent_issuer": agent_issuer, "workspace_issuer": workspace_issuer}


def require_platform_reachable(agent: Agent, workspace: WorkspaceContext, caller: str) -> None:
    """Refuse, by name, a platform invoke the agent's authorizer cannot accept.

    Every token the platform presents is issued by the workspace pool, so a JWT
    agent deployed against another IdP would answer a bare 403. Checked before
    a token is minted; ``caller`` (``user_jwt`` | ``m2m``) is echoed so the
    console can say which path was refused.
    """
    mismatch = issuer_mismatch(deployed_inbound_auth(agent), workspace)
    if mismatch is None:
        return
    raise AppError(
        "agent.inbound_issuer_mismatch",
        f"this agent's JWT authorizer trusts {mismatch['agent_issuer']}, but "
        f"Launchpad presents tokens of the workspace Cognito pool "
        f"({mismatch['workspace_issuer']}) — console Chat, /v1, direct invoke and "
        "evaluation cannot reach it; call its Runtime endpoint with a token from "
        "its own IdP",
        {**mismatch, "caller": caller},
        status_code=409,
    )


def suggested_cognito_default(workspace: WorkspaceContext) -> dict[str, Any] | None:
    """A ready-to-use JWT config for the workspace's own Cognito pool, or None.

    Lists both clients the platform itself uses so console Chat (user JWT via
    ``policy_identity.gateway_user_token`` — the *console* app client) and the
    non-interactive M2M path both pass ``allowedClients`` out of the box. The
    UI offers this as the one-click Cognito preset.
    """
    pool_id = str(workspace.resources.get("user_pool_id") or "")
    if not pool_id:
        return None
    clients = [
        client_id
        for client_id in (
            str(workspace.resources.get("user_pool_client_id") or ""),
            str(workspace.resources.get("m2m_client_id") or ""),
        )
        if client_id
    ]
    if not clients:
        return None
    return {
        "discovery_url": (
            f"https://cognito-idp.{workspace.region}.amazonaws.com/{pool_id}"
            "/.well-known/openid-configuration"
        ),
        "allowed_clients": clients,
        "allowed_audience": [],
        "allowed_scopes": [],
        "custom_claims": [],
    }


# ── caller tokens for JWT-mode agents ────────────────────────────────────────

_m2m_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
_m2m_lock = threading.Lock()


def m2m_bearer_token(workspace: WorkspaceContext) -> str:
    """A client_credentials access token from the workspace's Cognito M2M client.

    The non-interactive caller identity (public /v1 API, evaluation runs,
    canaries) for JWT-mode agents. The client secret is read from Cognito at
    mint time and never persisted; tokens are cached until shortly before
    expiry. Raises a named AppError when the workspace has no M2M client —
    callers must surface that instead of falling back to SigV4 (which the
    JWT-mode runtime would just 403).
    """
    pool_id = str(workspace.resources.get("user_pool_id") or "")
    client_id = str(workspace.resources.get("m2m_client_id") or "")
    if not (pool_id and client_id):
        raise AppError(
            "identity.m2m_unavailable",
            "this workspace has no Cognito M2M client (resources.m2m_client_id) — "
            "run its bootstrap, or invoke this JWT-mode agent with your own "
            "bearer token via the Runtime HTTPS endpoint",
            status_code=503,
        )
    key = (workspace.account_id, workspace.region, client_id)
    cached = _m2m_cache.get(key)
    if cached and float(cached["expires_at"]) > time.time() + 60:
        return str(cached["token"])
    with _m2m_lock:
        cached = _m2m_cache.get(key)
        if cached and float(cached["expires_at"]) > time.time() + 60:
            return str(cached["token"])
        cognito = workspace.client("cognito-idp")
        try:
            client = cognito.describe_user_pool_client(
                UserPoolId=pool_id, ClientId=client_id
            )["UserPoolClient"]
            secret = str(client.get("ClientSecret") or "")
            domain = _pool_domain(cognito, workspace, pool_id)
            token_endpoint = f"https://{domain}/oauth2/token"
            basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode("ascii")
            response = httpx.post(
                token_endpoint,
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {basic}"},
                timeout=10.0,
            )
            response.raise_for_status()
            body = response.json()
            token = str(body["access_token"])
            expires_in = float(body.get("expires_in") or 3600)
        except AppError:
            raise
        except Exception as exc:
            raise AppError(
                "identity.m2m_token_failed",
                "could not mint a machine (client_credentials) token from the "
                f"workspace M2M client: {type(exc).__name__}: {exc}",
                status_code=502,
            ) from exc
        _m2m_cache[key] = {"token": token, "expires_at": time.time() + expires_in}
        return token


def _pool_domain(cognito: Any, workspace: WorkspaceContext, pool_id: str) -> str:
    """The pool's hosted-UI domain host — where /oauth2/token lives.

    ``user_pool_domain`` is recorded by console-registered workspace bootstraps;
    the hub's CDK pool predates that key, so fall back to DescribeUserPool.
    """
    recorded = str(workspace.resources.get("user_pool_domain") or "")
    if recorded:
        return f"{recorded}.auth.{workspace.region}.amazoncognito.com"
    described = cognito.describe_user_pool(UserPoolId=pool_id).get("UserPool") or {}
    domain = str(described.get("Domain") or "")
    if not domain:
        raise AppError(
            "identity.m2m_unavailable",
            "the workspace's Cognito pool has no hosted-UI domain, so there is "
            "no token endpoint for client_credentials — add a domain to the "
            "pool or invoke with your own bearer token",
            status_code=503,
        )
    custom = described.get("CustomDomain")
    if custom:
        return str(custom)
    return f"{domain}.auth.{workspace.region}.amazoncognito.com"
