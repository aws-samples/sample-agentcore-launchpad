"""Gateway targets bound to a Connection (outbound auth performed by the gateway).

A target on the workspace gateway (`resources.gateway_id`) carries
`credentialProviderConfigurations` built from one Connection, so any agent that
attaches the gateway — of any creation method — gets the target's outbound
auth with no code change. AWS is the source of truth: the binding is read back
from `GetGatewayTarget`, never stored in the ledger.

Target type ↔ outbound auth (devguide gateway-outbound-auth, cross-checked in
docs/identity.md §1.4): OpenAPI takes OAuth client credentials or an API key;
an MCP server takes OAuth only. Lambda / Smithy targets never take a Connection.
"""

import json
import re
from typing import Any

import yaml
from botocore.exceptions import ClientError

from app.core.errors import AppError, NotFoundError
from app.schemas.inbound_auth import issuer_from_discovery_url
from app.services import consent_portals
from app.services import identity_providers as connections
from app.services.gateway_bootstrap import FACTS_TARGET_NAME, HR_TARGET_NAME

# the service model's TargetName pattern: ([0-9a-zA-Z][-]?){1,100}
TARGET_NAME_RE = re.compile(r"^([0-9a-zA-Z][-]?){1,100}$")

# bootstrap-owned targets on the workspace gateway; never deletable here
SYSTEM_TARGET_NAMES = frozenset({HR_TARGET_NAME, FACTS_TARGET_NAME})

SOURCE_OPENAPI = "openapi"
SOURCE_MCP = "mcp"


def _gateway_id(resources: dict[str, Any]) -> str:
    gateway_id = resources.get("gateway_id")
    if not gateway_id:
        raise AppError(
            "identity.no_gateway",
            "this workspace has no gateway (gateway_id) — run bootstrap first",
            status_code=409,
        )
    return gateway_id


def _connection_of(arn: str) -> tuple[str, str] | None:
    """(kind, name) from a provider ARN's `.../<type>credentialprovider/<name>` tail."""
    parts = arn.split("/")
    if len(parts) < 2:
        return None
    family, name = parts[-2], parts[-1]
    if family == "oauth2credentialprovider":
        return connections.KIND_OAUTH2, name
    if family == "apikeycredentialprovider":
        return connections.KIND_API_KEY, name
    return None


def _binding(detail: dict[str, Any]) -> dict[str, Any]:
    """The outbound-auth binding one GetGatewayTarget response carries."""
    for config in detail.get("credentialProviderConfigurations") or []:
        kind_name = config.get("credentialProviderType")
        provider = config.get("credentialProvider") or {}
        if kind_name == "OAUTH":
            oauth = provider.get("oauthCredentialProvider") or {}
            parsed = _connection_of(oauth.get("providerArn", ""))
            grant = oauth.get("grantType") or "CLIENT_CREDENTIALS"
            mode = {"CLIENT_CREDENTIALS": "as_agent", "AUTHORIZATION_CODE": "as_user"}.get(
                grant, "obo"
            )
            return {
                "auth": "oauth2",
                "connection": parsed[1] if parsed else None,
                "mode": mode,
                "scopes": list(oauth.get("scopes") or []),
            }
        if kind_name == "API_KEY":
            api_key = provider.get("apiKeyCredentialProvider") or {}
            parsed = _connection_of(api_key.get("providerArn", ""))
            return {
                "auth": "api_key",
                "connection": parsed[1] if parsed else None,
                "mode": "as_agent",
                "scopes": [],
            }
        if kind_name:
            return {"auth": kind_name.lower(), "connection": None, "mode": None, "scopes": []}
    return {"auth": "none", "connection": None, "mode": None, "scopes": []}


def _source_type(detail: dict[str, Any]) -> str:
    mcp = (detail.get("targetConfiguration") or {}).get("mcp") or {}
    if "openApiSchema" in mcp:
        return SOURCE_OPENAPI
    if "mcpServer" in mcp:
        return SOURCE_MCP
    return next(iter(mcp), "unknown")


def _list_summaries(control: Any, gateway_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token = None
    while True:
        kwargs = {"gatewayIdentifier": gateway_id, "maxResults": 100} | (
            {"nextToken": token} if token else {}
        )
        page = control.list_gateway_targets(**kwargs)
        items.extend(page.get("items", []))
        token = page.get("nextToken")
        if not token:
            break
    return items


def _target_out(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "target_id": detail.get("targetId", ""),
        "name": detail.get("name", ""),
        "description": detail.get("description") or "",
        "status": detail.get("status", ""),
        "status_reasons": list(detail.get("statusReasons") or []),
        "source": _source_type(detail),
        "system": detail.get("name") in SYSTEM_TARGET_NAMES,
        **_binding(detail),
    }


def list_targets(control: Any, resources: dict[str, Any]) -> list[dict[str, Any]]:
    """Every target on the workspace gateway with its bound Connection.

    ListGatewayTargets summaries carry no credential config, so each target is
    read back through GetGatewayTarget (a workspace gateway holds a handful).
    """
    gateway_id = resources.get("gateway_id")
    if not gateway_id:
        return []
    out = []
    for summary in _list_summaries(control, gateway_id):
        try:
            detail = control.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=summary["targetId"]
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                continue  # deleted between list and get
            raise
        out.append(_target_out(detail))
    return out


def bound_targets_safe(control: Any, resources: dict[str, Any]) -> list[dict[str, Any]]:
    """`list_targets` for reference checks on read paths: a gateway read failure
    must not hide the Connections list. Delete paths use `list_targets` so a
    failure refuses rather than skipping the reference check."""
    try:
        return list_targets(control, resources)
    except ClientError:
        return []


def _openapi_payload(schema: str) -> str:
    try:
        parsed = yaml.safe_load(schema)
    except yaml.YAMLError as exc:
        raise AppError(
            "identity.invalid_openapi",
            f"the OpenAPI schema does not parse: {exc}",
            status_code=422,
        ) from None
    if not isinstance(parsed, dict) or not str(parsed.get("openapi", "")).startswith("3"):
        raise AppError(
            "identity.invalid_openapi",
            "the OpenAPI schema must be an OpenAPI 3.x document (JSON or YAML)",
            status_code=422,
        )
    servers = parsed.get("servers") or []
    url = servers[0].get("url", "") if servers and isinstance(servers[0], dict) else ""
    if not str(url).startswith("https://"):
        raise AppError(
            "identity.invalid_openapi",
            "the OpenAPI schema needs servers[0].url with an https:// URL",
            status_code=422,
        )
    return json.dumps(parsed)


def _credential_config(
    *,
    source: str,
    kind: str,
    provider_arn: str,
    scopes: list[str],
    api_key_location: str,
    api_key_parameter: str,
    api_key_prefix: str | None,
    return_url: str | None = None,
    obo: bool = False,
) -> dict[str, Any]:
    if kind == connections.KIND_OAUTH2:
        oauth: dict[str, Any] = {
            "providerArn": provider_arn,
            "scopes": scopes,
            "grantType": "CLIENT_CREDENTIALS",
        }
        if return_url:
            # as_user: the gateway fetches the user's own token; consent happens
            # on the workspace gateway's Consent Portal, which the IdP sends
            # back to via this URL (services/consent_portals.py)
            oauth["grantType"] = "AUTHORIZATION_CODE"
            oauth["defaultReturnUrl"] = return_url
        elif obo:
            # obo: the gateway exchanges the caller's inbound JWT at the
            # Connection's IdP (its onBehalfOfTokenExchangeConfig) for a token
            # scoped to this target — no consent, no vault grant
            oauth["grantType"] = "TOKEN_EXCHANGE"
        return {
            "credentialProviderType": "OAUTH",
            "credentialProvider": {"oauthCredentialProvider": oauth},
        }
    if source != SOURCE_OPENAPI:
        raise AppError(
            "identity.target_auth_unsupported",
            "an MCP server target takes an OAuth2 connection only — API keys bind "
            "to OpenAPI targets",
            status_code=422,
        )
    api_key: dict[str, Any] = {
        "providerArn": provider_arn,
        "credentialLocation": api_key_location,
        "credentialParameterName": api_key_parameter,
    }
    if api_key_prefix:
        api_key["credentialPrefix"] = api_key_prefix
    return {
        "credentialProviderType": "API_KEY",
        "credentialProvider": {"apiKeyCredentialProvider": api_key},
    }


def _require_obo(
    control: Any, gateway_id: str, connection: str, provider: dict[str, Any]
) -> dict[str, Any]:
    """An obo target needs a user JWT to exchange and a Connection that can.

    The gateway exchanges the *inbound* token, so an IAM-authorized gateway has
    nothing to exchange (409); the Connection must carry an
    onBehalfOfTokenExchangeConfig, created against an IdP that implements
    RFC 8693 / RFC 7523 (422).
    """
    gateway = control.get_gateway(gatewayIdentifier=gateway_id)
    authorizer = gateway.get("authorizerType")
    if authorizer != "CUSTOM_JWT":
        raise AppError(
            "identity.obo_needs_jwt_gateway",
            f"on-behalf-of exchanges the caller's inbound JWT, but this gateway "
            f"authorizes with {authorizer or 'no authorizer'} — it needs CUSTOM_JWT",
            detail={"authorizer_type": authorizer},
            status_code=409,
        )
    if connections.obo_config_out(provider) is None:
        raise AppError(
            "identity.obo_unsupported",
            f"connection {connection!r} is not configured for on-behalf-of token "
            "exchange — create a CustomOauth2 connection with an OBO grant against "
            "an IdP that implements RFC 8693 or RFC 7523 (docs/identity.md §8.3)",
            detail={"connection": connection},
            status_code=422,
        )
    return gateway


def obo_issuer_warnings(
    gateway: dict[str, Any], connection: str, provider: dict[str, Any]
) -> list[dict[str, Any]]:
    """A non-blocking hint when the Connection's IdP is not the gateway's inbound issuer.

    The gateway hands the IdP the caller's inbound token (issued by the
    gateway's authorizer issuer) as the subject token, so the IdP must trust
    that issuer. A different issuer is legitimate — a federated trust set up at
    the IdP — so this warns, never refuses. Unknown on either side: no warning.
    """
    jwt = ((gateway.get("authorizerConfiguration") or {}).get("customJWTAuthorizer")) or {}
    gateway_issuer = issuer_from_discovery_url(jwt.get("discoveryUrl"))
    connection_issuer = (connections.oidc_discovery(provider) or {}).get("issuer")
    if not (gateway_issuer and connection_issuer) or gateway_issuer == connection_issuer:
        return []
    return [{
        "code": "identity.obo_issuer_mismatch",
        "message": (
            f"connection {connection!r} exchanges at {connection_issuer}, but the "
            f"gateway's inbound tokens are issued by {gateway_issuer} — the IdP "
            "must trust that issuer for the token exchange to succeed"
        ),
        "detail": {
            "connection": connection,
            "connection_issuer": connection_issuer,
            "gateway_issuer": gateway_issuer,
        },
    }]


def create_target(
    control: Any,
    resources: dict[str, Any],
    *,
    name: str,
    description: str | None,
    source: str,
    openapi_schema: str | None,
    mcp_endpoint: str | None,
    connection: str,
    kind: str,
    mode: str,
    scopes: list[str],
    api_key_location: str = "HEADER",
    api_key_parameter: str = "Authorization",
    api_key_prefix: str | None = None,
) -> dict[str, Any]:
    """Create one target bound to a Connection. Returns without waiting for
    READY — the list re-reads the status (MCP targets sync tools first)."""
    gateway_id = _gateway_id(resources)
    if mode not in connections.TARGET_MODES:
        raise AppError(
            "identity.mode_unsupported",
            f"acting mode {mode!r} is not available for gateway targets — "
            f"supported: {', '.join(connections.TARGET_MODES)}",
            status_code=422,
        )
    if not TARGET_NAME_RE.match(name):
        raise AppError(
            "identity.invalid_target_name",
            "target names are letters, digits and single dashes (≤ 100)",
            status_code=422,
        )
    if source == SOURCE_OPENAPI:
        if not openapi_schema:
            raise AppError(
                "identity.invalid_openapi", "an OpenAPI schema is required", status_code=422
            )
        target_config: dict[str, Any] = {
            "mcp": {"openApiSchema": {"inlinePayload": _openapi_payload(openapi_schema)}}
        }
    elif source == SOURCE_MCP:
        if not mcp_endpoint or not mcp_endpoint.startswith("https://"):
            raise AppError(
                "identity.invalid_mcp_endpoint",
                "an MCP server target needs an https:// endpoint",
                status_code=422,
            )
        target_config = {"mcp": {"mcpServer": {"endpoint": mcp_endpoint}}}
    else:
        raise AppError(
            "identity.target_auth_unsupported",
            f"target source {source!r} does not take a connection",
            status_code=422,
        )
    if mode in ("as_user", "obo") and kind != connections.KIND_OAUTH2:
        raise AppError(
            "identity.target_auth_unsupported",
            f"acting mode {mode!r} takes an OAuth2 connection",
            status_code=422,
        )
    credential = _credential_config(
        source=source,
        kind=kind,
        provider_arn="",  # placeholder until resolved below (shape checks first)
        scopes=scopes,
        api_key_location=api_key_location,
        api_key_parameter=api_key_parameter,
        api_key_prefix=api_key_prefix,
        # "-" placeholder: the portal lookup is an AWS read, done after the
        # shape checks like the provider ARN
        return_url="-" if mode == "as_user" else None,
        obo=mode == "obo",
    )
    if name in {t.get("name") for t in _list_summaries(control, gateway_id)}:
        raise AppError(
            "identity.target_exists",
            f"a gateway target named {name!r} already exists",
            status_code=409,
        )
    provider = connections.live_provider(control, kind, connection)
    arn = provider.get("credentialProviderArn", "")
    warnings: list[dict[str, Any]] = []
    if mode == "obo":
        gateway = _require_obo(control, gateway_id, connection, provider)
        warnings = obo_issuer_warnings(gateway, connection, provider)
    provider_key = next(iter(credential["credentialProvider"]))
    credential["credentialProvider"][provider_key]["providerArn"] = arn
    if mode == "as_user":
        credential["credentialProvider"][provider_key]["defaultReturnUrl"] = (
            consent_portals.active_return_url(control, resources)
        )
    kwargs: dict[str, Any] = {
        "gatewayIdentifier": gateway_id,
        "name": name,
        "targetConfiguration": target_config,
        "credentialProviderConfigurations": [credential],
    }
    if description:
        kwargs["description"] = description
    try:
        created = control.create_gateway_target(**kwargs)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConflictException":
            raise AppError(
                "identity.target_exists",
                f"a gateway target named {name!r} already exists",
                status_code=409,
            ) from None
        raise
    out = _target_out(
        {
            "targetId": created.get("targetId", ""),
            "name": name,
            "description": description,
            "status": created.get("status", "CREATING"),
            "targetConfiguration": target_config,
            "credentialProviderConfigurations": [credential],
        }
    )
    # non-blocking hints about the created target (obo issuer trust)
    out["warnings"] = warnings
    return out


def delete_target(control: Any, resources: dict[str, Any], target_id: str) -> None:
    gateway_id = _gateway_id(resources)
    try:
        detail = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            raise NotFoundError(
                "identity.target_not_found", f"no gateway target {target_id!r}"
            ) from None
        raise
    if detail.get("name") in SYSTEM_TARGET_NAMES:
        raise AppError(
            "identity.system_target",
            f"{detail.get('name')!r} is a platform target managed by bootstrap",
            status_code=409,
        )
    if _binding(detail)["connection"] is None:
        # this surface manages Connection-bound targets only; others belong to
        # the surface that created them (Registry, KB mounts, bootstrap)
        raise AppError(
            "identity.target_unbound",
            f"target {detail.get('name')!r} is not bound to a connection",
            status_code=409,
        )
    control.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
