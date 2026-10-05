"""Connections (`/api/identity/connections`), Connection-bound Gateway targets
(`/api/identity/gateway-targets`), the as_user (3LO) consent legs and grants
(`/api/identity/oauth/complete`, `/api/identity/grants`) and the gateway's
Consent Portal (`/api/identity/consent-portal`), and the workspace's
inbound-auth default (`/api/identity/inbound-auth/default`, P3).

Thin HTTP shell over `services.identity_providers` and
`services.identity_gateway_targets`. The secret fields (`client_secret`,
`api_key`) exist only in the request body and the outbound AWS call; they are
never written to the ledger, a log, or a response.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.models.ledger import Agent, Workspace
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import CONNECTION_NAME_RE
from app.schemas.inbound_auth import InboundAuth
from app.services import consent_portals, inbound_auth, oauth_sessions, policy_identity
from app.services import identity_gateway_targets as gateway_targets
from app.services import identity_providers as connections
from app.services.agentcore.client import control_client, data_client

router = APIRouter(prefix="/api/identity", tags=["identity"])

Vendor = Literal[
    "CustomOauth2",
    "GoogleOauth2",
    "GithubOauth2",
    "SlackOauth2",
    "SalesforceOauth2",
    "MicrosoftOauth2",
    "AtlassianOauth2",
    "LinkedinOauth2",
]
Kind = Literal["oauth2", "api_key"]


class OboConfigIn(BaseModel):
    """On-behalf-of exchange a CustomOauth2 Connection performs (P3)."""

    grant_type: Literal["TOKEN_EXCHANGE", "JWT_AUTHORIZATION_GRANT"] = "TOKEN_EXCHANGE"
    actor_token_content: Literal["NONE", "M2M"] = "NONE"
    actor_token_scopes: list[str] = Field(default_factory=list, max_length=32)


class CreateOauth2Request(BaseModel):
    name: str = Field(pattern=CONNECTION_NAME_RE)
    vendor: Vendor = "CustomOauth2"
    template: str | None = Field(default=None, max_length=32)
    description: str | None = Field(default=None, max_length=200)
    client_id: str = Field(min_length=1, max_length=256)
    client_secret: str = Field(min_length=1, max_length=2048)
    # CustomOauth2 endpoint discovery: the .well-known URL, or all three explicit
    # endpoints. Vendor-typed providers derive endpoints themselves.
    discovery_url: str | None = Field(default=None, max_length=1024)
    authorization_endpoint: str | None = Field(default=None, max_length=1024)
    token_endpoint: str | None = Field(default=None, max_length=1024)
    issuer: str | None = Field(default=None, max_length=1024)
    # the scope range this Connection is meant for (display + defaults); the
    # exchange itself takes scopes per tool / per target
    scopes: list[str] = Field(default_factory=list, max_length=32)
    obo: OboConfigIn | None = None


class CreateApiKeyRequest(BaseModel):
    name: str = Field(pattern=CONNECTION_NAME_RE)
    description: str | None = Field(default=None, max_length=200)
    api_key: str = Field(min_length=1, max_length=65536)


class ApiKeyPlacementIn(BaseModel):
    location: Literal["HEADER", "QUERY_PARAMETER"] = "HEADER"
    parameter_name: str = Field(default="Authorization", min_length=1, max_length=64)
    prefix: str | None = Field(default=None, max_length=64)


class CreateTargetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=200)
    source: Literal["openapi", "mcp"]
    openapi_schema: str | None = Field(default=None, max_length=1_000_000)
    mcp_endpoint: str | None = Field(default=None, max_length=1024)
    connection: str = Field(pattern=CONNECTION_NAME_RE)
    kind: Kind
    mode: Literal["as_agent", "as_user", "obo"] = "as_agent"
    scopes: list[str] = Field(default_factory=list, max_length=32)
    api_key: ApiKeyPlacementIn = Field(default_factory=ApiKeyPlacementIn)


def _caller(request: Request) -> str:
    return getattr(require_identity(request), "username", "") or ""


def _resources(ws: WorkspaceScope) -> dict[str, Any]:
    return ws.context.resources or {}


# ── Connections ──────────────────────────────────────────────────────────────


@router.get("/connections")
def list_connections(
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    control = control_client(ws.context)
    bound = gateway_targets.bound_targets_safe(control, _resources(ws))
    return {
        "connections": connections.list_connections(control, db, ws.id, bound_targets=bound)
    }


# registered before `/connections/{kind}/{name}` only for readability: the
# literal path has two segments, the capture three, so they cannot collide
@router.get("/connections/templates")
def list_templates() -> dict[str, Any]:
    return {"templates": list(connections.TEMPLATES)}


@router.get("/connections/oidc-sources")
def list_oidc_sources(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    """OAuth2 Connections with a derivable OIDC discovery URL — the inbound JWT
    form's "Choose from a Connection" picker. The Connection's own client id is
    deliberately not returned: it is the agent's outbound client, not a caller."""
    return {"sources": connections.list_oidc_sources(control_client(ws.context))}


@router.get("/connections/{kind}/{name}")
def get_connection(
    kind: Kind,
    name: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    control = control_client(ws.context)
    bound = gateway_targets.bound_targets_safe(control, _resources(ws))
    return connections.get_connection(control, db, ws.id, kind, name, bound_targets=bound)


@router.post("/connections/oauth2", status_code=201)
def create_oauth2(
    req: CreateOauth2Request,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    template = req.template if req.template in connections.TEMPLATE_IDS else None
    return connections.create_oauth2_connection(
        control_client(ws.context),
        db,
        ws.id,
        name=req.name,
        vendor=req.vendor,
        client_id=req.client_id,
        client_secret=req.client_secret,
        discovery_url=req.discovery_url or None,
        authorization_endpoint=req.authorization_endpoint or None,
        token_endpoint=req.token_endpoint or None,
        issuer=req.issuer or None,
        scopes=[s for s in (x.strip() for x in req.scopes) if s],
        template=template,
        description=req.description or None,
        created_by=_caller(request),
        obo=req.obo.model_dump() if req.obo else None,
    )


@router.post("/connections/api-key", status_code=201)
def create_api_key(
    req: CreateApiKeyRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    return connections.create_api_key_connection(
        control_client(ws.context),
        db,
        ws.id,
        name=req.name,
        api_key=req.api_key,
        description=req.description or None,
        created_by=_caller(request),
    )


@router.delete("/connections/{kind}/{name}")
def delete_connection(
    kind: Kind,
    name: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    control = control_client(ws.context)
    # not the `_safe` variant: a gateway read failure must refuse the delete
    # rather than skip the target-reference check
    bound = gateway_targets.list_targets(control, _resources(ws))
    connections.delete_connection(control, db, ws.id, kind, name, bound_targets=bound)
    return {"deleted": True}


# ── Gateway targets bound to a Connection ────────────────────────────────────


@router.get("/gateway-targets")
def list_gateway_targets(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    resources = _resources(ws)
    return {
        "gateway_id": resources.get("gateway_id") or None,
        "targets": gateway_targets.list_targets(control_client(ws.context), resources),
    }


@router.post("/gateway-targets", status_code=201)
def create_gateway_target(
    req: CreateTargetRequest, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    return gateway_targets.create_target(
        control_client(ws.context),
        _resources(ws),
        name=req.name,
        description=req.description or None,
        source=req.source,
        openapi_schema=req.openapi_schema,
        mcp_endpoint=req.mcp_endpoint,
        connection=req.connection,
        kind=req.kind,
        mode=req.mode,
        scopes=[s for s in (x.strip() for x in req.scopes) if s],
        api_key_location=req.api_key.location,
        api_key_parameter=req.api_key.parameter_name,
        api_key_prefix=req.api_key.prefix or None,
    )


@router.delete("/gateway-targets/{target_id}")
def delete_gateway_target(
    target_id: str, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    gateway_targets.delete_target(control_client(ws.context), _resources(ws), target_id)
    return {"deleted": True}


# ── as_user (3LO): consent completion, my grants, revoke ─────────────────────


class CompleteConsentRequest(BaseModel):
    # the `session_id` AgentCore Identity appended to the return URL — the
    # sessionUri of GetResourceOauth2Token, an opaque one-time capability
    session_id: str = Field(min_length=1, max_length=512)


@router.post("/oauth/complete")
def complete_consent(
    req: CompleteConsentRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Leg 3 of 3LO: bind the consented token to the user recorded for this
    session. The caller must be that user (oauth_sessions.complete)."""
    identity = require_identity(request)

    def user_token(kind: str) -> str | None:
        # A JWT-inbound ask is completed with the caller's own user JWT — the
        # subject the runtime saw. An ask over the shared M2M subject is never
        # completable (oauth_sessions refuses it before asking for a token).
        if kind != oauth_sessions.CALLER_USER_JWT:
            return None
        return policy_identity.gateway_user_token(
            ws.context, identity.username, identity.role, identity.email,
        )

    result = oauth_sessions.complete(
        db,
        ws.id,
        req.session_id,
        caller=_caller(request),
        data_client=data_client(ws.context),
        user_token=user_token,
    )
    agent = db.get(Agent, result["agent_id"]) if result["agent_id"] else None
    return {**result, "agent_name": agent.name if agent is not None else None}


def _agent_names(db: Session, ids: set[str]) -> dict[str, str]:
    if not ids:
        return {}
    return {a.id: a.name for a in db.query(Agent).filter(Agent.id.in_(ids))}


@router.get("/grants")
def list_my_grants(
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The caller's own as_user grants — never another member's."""
    grants = oauth_sessions.list_grants(db, ws.id, user_id=_caller(request))
    names = _agent_names(db, {g["agent_id"] for g in grants if g["agent_id"]})
    return {"grants": [{**g, "agent_name": names.get(g["agent_id"])} for g in grants]}


@router.get("/grants/{connection}/status")
def grant_status(
    request: Request,
    connection: str = Path(pattern=CONNECTION_NAME_RE),
    agent_id: str = "",
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """What the Chat auth card polls until the consent completes."""
    return oauth_sessions.grant_status(
        db, ws.id, user_id=_caller(request), provider=connection, agent_id=agent_id
    )


@router.delete("/grants/{connection}")
def revoke_grant(
    request: Request,
    connection: str = Path(pattern=CONNECTION_NAME_RE),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Force re-auth: the next call through this Connection restarts consent."""
    return oauth_sessions.revoke(db, ws.id, user_id=_caller(request), provider=connection)


# ── Consent Portal (as_user Gateway targets) ─────────────────────────────────


class CreateConsentPortalRequest(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    description: str | None = Field(default=None, max_length=200)
    # the inbound IdP as an OAuth2 Connection (same issuer as the gateway's
    # JWT authorizer)
    connection: str = Field(pattern=CONNECTION_NAME_RE)
    scopes: list[str] = Field(default_factory=lambda: ["openid"], min_length=1, max_length=32)
    audience: str | None = Field(default=None, max_length=512)
    execution_role_arn: str = Field(min_length=20, max_length=2048)


@router.get("/consent-portal")
def get_consent_portal(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    return consent_portals.status(control_client(ws.context), _resources(ws))


@router.post("/consent-portal", status_code=202)
def create_consent_portal(
    req: CreateConsentPortalRequest, ws: WorkspaceScope = Depends(require_workspace)
) -> dict[str, Any]:
    return consent_portals.create(
        control_client(ws.context),
        _resources(ws),
        name=req.name,
        connection=req.connection,
        scopes=[s for s in (x.strip() for x in req.scopes) if s],
        execution_role_arn=req.execution_role_arn,
        account_id=ws.context.account_id,
        region=ws.context.region,
        audience=req.audience or None,
        description=req.description or None,
    )


@router.delete("/consent-portal")
def delete_consent_portal(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    consent_portals.delete(control_client(ws.context), _resources(ws))
    return {"deleted": True}


# ── inbound auth (P3) ────────────────────────────────────────────────────────


def _default_out(ws: WorkspaceScope) -> dict[str, Any]:
    stored = inbound_auth.workspace_default(ws.row)
    return {
        "workspace_id": ws.id,
        "default": (stored or InboundAuth(mode="iam")).model_dump(),
        # a workspace with no stored blob is implicitly IAM; the UI shows that
        "configured": stored is not None,
        # bootstrap wiring the console/M2M path needs: what a ready-to-use
        # Cognito default would look like for this workspace
        "cognito": inbound_auth.suggested_cognito_default(ws.context),
        # the issuer of every token the console, /v1 and evaluation present; a
        # JWT config on another issuer is unreachable from them (the UI warns)
        "cognito_issuer": inbound_auth.workspace_cognito_issuer(ws.context),
    }


@router.get("/inbound-auth/default")
def get_inbound_default(ws: WorkspaceScope = Depends(require_workspace)) -> dict[str, Any]:
    return _default_out(ws)


@router.put("/inbound-auth/default")
def put_inbound_default(
    auth: InboundAuth,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Set the workspace's inbound-auth default.

    Probes the discovery document before persisting a JWT config, so a typo'd
    URL fails here with the reason instead of 403ing every future deploy's
    callers. Deployed agents keep their current authorizer until redeployed.
    """
    inbound_auth.validate_inbound_auth(auth)
    # ws.row is detached (resolved in a closed session) — write via this
    # request's session and echo the stored value from the same row.
    row = db.get(Workspace, ws.id)
    inbound_auth.set_workspace_default(db, row, auth)
    stored = inbound_auth.workspace_default(row)
    return {
        "workspace_id": ws.id,
        "default": (stored or InboundAuth(mode="iam")).model_dump(),
        "configured": stored is not None,
        "cognito": inbound_auth.suggested_cognito_default(ws.context),
        # the issuer of every token the console, /v1 and evaluation present; a
        # JWT config on another issuer is unreachable from them (the UI warns)
        "cognito_issuer": inbound_auth.workspace_cognito_issuer(ws.context),
    }
