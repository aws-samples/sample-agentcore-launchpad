"""Workspace-scoped Connections: AgentCore Identity credential providers.

A *Connection* is the product shell over one credential provider (OAuth2 or API
key) in the workspace's token vault. AWS is the source of truth. The ledger row
(`models.ledger.IdentityProvider`) is an audit record holding identifiers only.
The secret material (`client_secret` / `api_key`) is passed straight through to
`create_*_credential_provider` and is never persisted, logged, or echoed here.

Two system providers are server-owned and cannot be deleted through this
surface: the Gateway M2M provider (`launchpad-gw-m2m`) and the sample REST API
key provider (`launchpad-office-facts-key`), both created by bootstrap
(`services/gateway_bootstrap.py`).

Vendor shapes come from the botocore `bedrock-agentcore-control` service model
(botocore 1.43.83, read 2026-09-28; see docs/identity.md §1), not guessed.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from botocore.exceptions import ClientError
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent, IdentityProvider
from app.schemas.inbound_auth import (
    discovery_url_for_issuer,
    issuer_from_discovery_url,
    normalize_issuer,
)
from app.services import inbound_auth, oauth_sessions
from app.services.gateway_bootstrap import (
    API_KEY_PROVIDER_NAME,
    GATEWAY_M2M_PROVIDER_NAME,
)

logger = logging.getLogger("launchpad.identity_providers")

# Connection kinds as this API names them (the vault calls them
# oauth2credentialprovider / apikeycredentialprovider in the ARN).
KIND_OAUTH2 = "oauth2"
KIND_API_KEY = "api_key"
KINDS = (KIND_OAUTH2, KIND_API_KEY)

# Vault-owned providers the platform depends on; never deletable here.
SYSTEM_PROVIDER_NAMES = frozenset({GATEWAY_M2M_PROVIDER_NAME, API_KEY_PROVIDER_NAME})

# Acting modes Launchpad knows, and where each executes: `as_user` (3LO)
# shipped in P2; `obo` (token exchange, P3) is performed by the gateway, so it
# is a gateway-target mode only — a code tool would need the Runtime to call
# GetResourceOauth2Token(ON_BEHALF_OF_TOKEN_EXCHANGE) itself.
ACTING_MODES = ("as_agent", "as_user", "obo")
SUPPORTED_MODES = ("as_agent", "as_user")
TARGET_MODES = ACTING_MODES

# onBehalfOfTokenExchangeConfig.grantType → the grant URN the IdP must accept
# (devguide on-behalf-of-token-exchange; botocore OnBehalfOfTokenExchangeGrantTypeType)
OBO_GRANT_URNS = {
    "TOKEN_EXCHANGE": "urn:ietf:params:oauth:grant-type:token-exchange",  # RFC 8693
    "JWT_AUTHORIZATION_GRANT": "urn:ietf:params:oauth:grant-type:jwt-bearer",  # RFC 7523
}
# ActorTokenContentType members offered; AWS_IAM_ID_TOKEN_JWT needs account-level
# outbound web identity federation and is left out
OBO_ACTOR_CONTENTS = ("NONE", "M2M")

# CredentialProviderVendorType members the console offers, mapped to the
# oauth2ProviderConfigInput key that carries their config. CustomOauth2 is the
# only member whose input takes endpoints; the others take {clientId,
# clientSecret}. The newer vendors (Okta, Auth0, Cognito, ...) ride
# `includedOauth2ProviderConfig`; they are refused rather than guessed.
VENDOR_CONFIG_KEYS: dict[str, str] = {
    "CustomOauth2": "customOauth2ProviderConfig",
    "GoogleOauth2": "googleOauth2ProviderConfig",
    "GithubOauth2": "githubOauth2ProviderConfig",
    "SlackOauth2": "slackOauth2ProviderConfig",
    "SalesforceOauth2": "salesforceOauth2ProviderConfig",
    "MicrosoftOauth2": "microsoftOauth2ProviderConfig",
    "AtlassianOauth2": "atlassianOauth2ProviderConfig",
    "LinkedinOauth2": "linkedinOauth2ProviderConfig",
}

# Creation templates the console offers. `fields` lists the inputs the form
# shows; the console localizes labels by template id (`v2.connections.tpl.<id>`).
# `discovery_hint` is a placeholder only, never a default.
TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "cognito",
        "kind": KIND_OAUTH2,
        "vendor": "CustomOauth2",
        "fields": ["client_id", "client_secret", "discovery_url", "scopes"],
        "discovery_hint": (
            "https://cognito-idp.<region>.amazonaws.com/<user-pool-id>"
            "/.well-known/openid-configuration"
        ),
    },
    {
        "id": "custom_oidc",
        "kind": KIND_OAUTH2,
        "vendor": "CustomOauth2",
        "fields": ["client_id", "client_secret", "discovery_url", "scopes"],
        "discovery_hint": "https://idp.example.com/.well-known/openid-configuration",
    },
    {
        "id": "custom_endpoints",
        "kind": KIND_OAUTH2,
        "vendor": "CustomOauth2",
        "fields": [
            "client_id",
            "client_secret",
            "issuer",
            "authorization_endpoint",
            "token_endpoint",
            "scopes",
        ],
    },
    *(
        {
            "id": vendor.removesuffix("Oauth2").lower(),
            "kind": KIND_OAUTH2,
            "vendor": vendor,
            "fields": ["client_id", "client_secret", "scopes"],
        }
        for vendor in (
            "GithubOauth2",
            "GoogleOauth2",
            "SlackOauth2",
            "SalesforceOauth2",
            "MicrosoftOauth2",
            "AtlassianOauth2",
            "LinkedinOauth2",
        )
    ),
    {"id": "api_key", "kind": KIND_API_KEY, "vendor": "", "fields": ["api_key"]},
)
TEMPLATE_IDS = frozenset(t["id"] for t in TEMPLATES)


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return str(value) if value else ""


def _aws_code(exc: ClientError) -> str:
    return exc.response.get("Error", {}).get("Code", "")


def _paginate(list_call: Any, *, page_size: int = 20, **kwargs: Any) -> list[dict[str, Any]]:
    """Drain a list_*_credential_providers call across all pages.

    ``ListOauth2CredentialProviders`` declares maxResults 1–20 in the service
    model (the api-key list allows 100). 20 is the safe bound for both.
    """
    items: list[dict[str, Any]] = []
    token = None
    while True:
        page_kwargs = {"maxResults": page_size, **kwargs} | (
            {"nextToken": token} if token else {}
        )
        page = list_call(**page_kwargs)
        items.extend(page.get("credentialProviders", []))
        token = page.get("nextToken")
        if not token:
            break
    return items


def provider_names(control: Any, kind: str) -> set[str]:
    """Live provider names of one kind — the validation set for ToolAuth."""
    if kind == KIND_OAUTH2:
        return {p["name"] for p in _paginate(control.list_oauth2_credential_providers)}
    return {p["name"] for p in _paginate(control.list_api_key_credential_providers)}


def _tool_connection(tool: Any) -> str | None:
    """The Connection a stored tool dict's auth block names (legacy `provider`)."""
    auth = tool.get("auth") if isinstance(tool, dict) else None
    if not isinstance(auth, dict):
        return None
    name = auth.get("connection") or auth.get("provider")
    return name if isinstance(name, str) else None


def referencing_agents(db: Session, workspace_id: str, name: str) -> list[dict[str, str]]:
    """Agents in this workspace whose stored spec carries a tool auth'd by `name`.

    Read from the stored spec JSON, not AgentSpec — discovered/foreign rows may
    not validate, and a reference check must still see them.
    """
    rows = (
        db.query(Agent)
        .filter(Agent.workspace_id == workspace_id, Agent.status != "deleted")
        .all()
    )
    hits: list[dict[str, str]] = []
    for agent in rows:
        tools = (agent.spec or {}).get("tools") or []
        if any(_tool_connection(tool) == name for tool in tools):
            hits.append({"type": "agent", "id": agent.id, "name": agent.name})
    return hits


def _references(
    db: Session,
    workspace_id: str,
    name: str,
    bound_targets: list[dict[str, Any]],
) -> list[dict[str, str]]:
    refs = referencing_agents(db, workspace_id, name)
    refs.extend(
        {"type": "gateway_target", "id": t["target_id"], "name": t["name"]}
        for t in bound_targets
        if t.get("connection") == name
    )
    return refs


def _item(
    *,
    name: str,
    kind: str,
    row: IdentityProvider | None,
    aws: dict[str, Any] | None,
    refs: list[dict[str, str]],
) -> dict[str, Any]:
    """One Connection in the API shape. `aws is None` means the vault lacks it."""
    aws = aws or {}
    system = name in SYSTEM_PROVIDER_NAMES
    return {
        "name": name,
        "kind": kind,
        "vendor": aws.get("credentialProviderVendor") or (row.vendor if row else ""),
        "arn": aws.get("credentialProviderArn") or (row.arn if row else ""),
        "callback_url": aws.get("callbackUrl") or (row.callback_url if row else None),
        "client_id": row.client_id if row else None,
        "scopes": list(row.scopes or []) if row else [],
        "template": row.template if row else None,
        "description": row.description if row else None,
        "created_at": _iso(aws.get("createdTime")) or (_iso(row.created_at) if row else ""),
        "created_by": row.created_by if row else "",
        "system": system,
        # who manages it: the platform, Launchpad (audit row), or someone else
        "source": "system" if system else ("launchpad" if row else "external"),
        "status": "ready" if aws else "missing",
        # read from GetOauth2CredentialProvider only; list summaries omit it
        "obo": obo_config_out(aws),
        "referenced_by": refs,
    }


def obo_config_out(aws: dict[str, Any] | None) -> dict[str, Any] | None:
    """The OBO exchange a provider is configured for, in API shape, or None."""
    output = (aws or {}).get("oauth2ProviderConfigOutput") or {}
    obo = (output.get("customOauth2ProviderConfig") or {}).get(
        "onBehalfOfTokenExchangeConfig"
    )
    if not obo:
        return None
    exchange = obo.get("tokenExchangeGrantTypeConfig") or {}
    return {
        "grant_type": obo.get("grantType", ""),
        "actor_token_content": exchange.get("actorTokenContent"),
        "actor_token_scopes": list(exchange.get("actorTokenScopes") or []),
    }


# Vendors whose reported issuer does not sign the tokens a caller would present.
# GitHub's GetOauth2CredentialProvider output names
# https://token.actions.githubusercontent.com (read live 2026-09-29): the GitHub
# Actions OIDC issuer, while GitHub OAuth-app tokens are opaque, not JWTs.
NON_OIDC_ISSUER_VENDORS = frozenset({"GithubOauth2"})


def oidc_discovery(aws: dict[str, Any] | None) -> dict[str, str] | None:
    """The OIDC discovery URL a provider's stored config yields, or None.

    Read from ``oauth2ProviderConfigOutput.<vendor>.oauthDiscovery`` of
    GetOauth2CredentialProvider, which every vendor output carries
    (botocore 1.43.103): ``discoveryUrl`` as given, else
    ``authorizationServerMetadata.issuer`` + the well-known suffix. Nothing is
    derived from the vendor name alone — a provider whose output carries
    neither yields None.
    """
    aws = aws or {}
    if aws.get("credentialProviderVendor") in NON_OIDC_ISSUER_VENDORS:
        return None
    for config in ((aws.get("oauth2ProviderConfigOutput") or {}).values()):
        discovery = (config or {}).get("oauthDiscovery") or {}
        url = str(discovery.get("discoveryUrl") or "")
        issuer = issuer_from_discovery_url(url)
        if issuer:
            return {"discovery_url": url, "issuer": issuer, "derived_from": "discovery_url"}
        metadata = discovery.get("authorizationServerMetadata") or {}
        derived = discovery_url_for_issuer(str(metadata.get("issuer") or ""))
        if derived:
            return {
                "discovery_url": derived,
                "issuer": normalize_issuer(str(metadata["issuer"])) or "",
                "derived_from": "issuer",
            }
    return None


def list_oidc_sources(control: Any) -> list[dict[str, str]]:
    """The OAuth2 Connections an inbound JWT config can take its discovery URL from.

    System providers are left out (the workspace Cognito pool has its own
    preset). A provider that cannot be read is skipped, not fatal: this feeds
    a picker, and the discovery URL stays typeable by hand.
    """
    sources: list[dict[str, str]] = []
    for summary in _paginate(control.list_oauth2_credential_providers):
        name = summary.get("name") or ""
        if not name or name in SYSTEM_PROVIDER_NAMES:
            continue
        try:
            aws = _get_aws(control, KIND_OAUTH2, name)
        except ClientError as exc:
            logger.warning("skipping connection %s: %s", name, _aws_code(exc))
            continue
        derived = oidc_discovery(aws)
        if derived:
            vendor = (aws or {}).get("credentialProviderVendor") or ""
            sources.append({"name": name, "vendor": vendor, **derived})
    return sorted(sources, key=lambda s: s["name"])


def _ledger_row(
    db: Session, workspace_id: str, kind: str, name: str
) -> IdentityProvider | None:
    return (
        db.query(IdentityProvider)
        .filter(
            IdentityProvider.workspace_id == workspace_id,
            IdentityProvider.kind == kind,
            IdentityProvider.name == name,
        )
        .first()
    )


def list_connections(
    control: Any,
    db: Session,
    workspace_id: str,
    *,
    bound_targets: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Merged Connection list: AWS is authoritative, the ledger adds audit fields.

    A ledger row with no AWS counterpart is returned with status="missing"
    (deleted out-of-band) rather than hidden — an operator should see that the
    audit trail and the vault disagree. An AWS provider with no row reads
    source="external".
    """
    targets = bound_targets or []
    ledger = {
        (row.kind, row.name): row
        for row in db.query(IdentityProvider)
        .filter(IdentityProvider.workspace_id == workspace_id)
        .all()
    }
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    live = [
        (KIND_OAUTH2, _paginate(control.list_oauth2_credential_providers)),
        (KIND_API_KEY, _paginate(control.list_api_key_credential_providers)),
    ]
    for kind, providers in live:
        for aws in providers:
            name = aws["name"]
            seen.add((kind, name))
            items.append(
                _item(
                    name=name,
                    kind=kind,
                    row=ledger.get((kind, name)),
                    aws=aws,
                    refs=_references(db, workspace_id, name, targets),
                )
            )
    for (kind, name), row in sorted(ledger.items()):
        if (kind, name) not in seen:
            items.append(
                _item(
                    name=name,
                    kind=kind,
                    row=row,
                    aws=None,
                    refs=_references(db, workspace_id, name, targets),
                )
            )
    return items


def _get_aws(control: Any, kind: str, name: str) -> dict[str, Any] | None:
    try:
        if kind == KIND_OAUTH2:
            return control.get_oauth2_credential_provider(name=name)
        return control.get_api_key_credential_provider(name=name)
    except ClientError as exc:
        if _aws_code(exc) == "ResourceNotFoundException":
            return None
        raise


def _require_kind(kind: str) -> None:
    if kind not in KINDS:
        raise NotFoundError(
            "identity.connection_not_found", f"unknown connection kind {kind!r}"
        )


def get_connection(
    control: Any,
    db: Session,
    workspace_id: str,
    kind: str,
    name: str,
    *,
    bound_targets: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One Connection, with the callback URL re-read from AWS (OAuth2)."""
    _require_kind(kind)
    aws = _get_aws(control, kind, name)
    row = _ledger_row(db, workspace_id, kind, name)
    if aws is None and row is None:
        raise NotFoundError(
            "identity.connection_not_found", f"no {kind} connection named {name!r}"
        )
    return _item(
        name=name,
        kind=kind,
        row=row,
        aws=aws,
        refs=_references(db, workspace_id, name, bound_targets or []),
    )


def live_provider(control: Any, kind: str, name: str) -> dict[str, Any]:
    """The live Get*CredentialProvider response, or 404 when the vault lacks it."""
    _require_kind(kind)
    aws = _get_aws(control, kind, name)
    if aws is None:
        raise NotFoundError(
            "identity.connection_not_found", f"no {kind} connection named {name!r}"
        )
    return aws


def resolve_arn(control: Any, kind: str, name: str) -> str:
    """The live provider ARN of one Connection, or 404 when the vault lacks it."""
    return live_provider(control, kind, name).get("credentialProviderArn", "")


def validate_tool_auth(control: Any, spec: Any) -> None:
    """Request-time check of every ``ToolRef.auth`` against the live vault.

    Deliberately here and not in the Pydantic schema: the valid Connection-name
    space lives in AWS, and a stored spec must keep loading after its
    Connection is deleted out-of-band (the next save fails loudly instead).
    """
    wanted: dict[str, tuple[str, str]] = {}
    for tool in getattr(spec, "tools", []) or []:
        auth = getattr(tool, "auth", None)
        if auth is None:
            continue
        if auth.mode not in SUPPORTED_MODES:
            raise AppError(
                "identity.mode_unsupported",
                f"tool {tool.name!r}: acting mode {auth.mode!r} is not available on a "
                f"tool — supported: {', '.join(SUPPORTED_MODES)}"
                + (" (obo runs on a gateway target bound to the connection)"
                   if auth.mode == "obo" else ""),
                detail={"tool": tool.name, "mode": auth.mode},
                status_code=422,
            )
        wanted[auth.connection] = (auth.kind, tool.name)
    if not wanted:
        return
    names = {kind: provider_names(control, kind) for kind in KINDS}
    for name, (kind, tool_name) in sorted(wanted.items()):
        if name in names[kind]:
            continue
        other = KIND_API_KEY if kind == KIND_OAUTH2 else KIND_OAUTH2
        if name in names[other]:
            raise AppError(
                "identity.kind_mismatch",
                f"tool {tool_name!r}: connection {name!r} is an {other} connection, "
                f"but the tool's auth declares kind {kind!r}",
                detail={"tool": tool_name, "connection": name, "kind": other},
                status_code=422,
            )
        raise AppError(
            "identity.connection_unknown",
            f"tool {tool_name!r} references connection {name!r}, which does not "
            "exist in this workspace — create it on the Connections page first",
            detail={"tool": tool_name, "connection": name},
            status_code=422,
        )


def _is_cognito(url: str | None) -> bool:
    host = urlparse(url or "").hostname or ""
    return host.startswith("cognito-idp.") or host.endswith(".amazoncognito.com")


def obo_config_input(obo: dict[str, Any]) -> dict[str, Any]:
    """`onBehalfOfTokenExchangeConfig` for CreateOauth2CredentialProvider."""
    grant = obo.get("grant_type") or "TOKEN_EXCHANGE"
    if grant not in OBO_GRANT_URNS:
        raise AppError(
            "identity.obo_invalid", f"unknown OBO grant type {grant!r}", status_code=422
        )
    config: dict[str, Any] = {"grantType": grant}
    if grant == "TOKEN_EXCHANGE":
        content = obo.get("actor_token_content") or "NONE"
        if content not in OBO_ACTOR_CONTENTS:
            raise AppError(
                "identity.obo_invalid",
                f"actor token content {content!r} is not offered — one of "
                f"{', '.join(OBO_ACTOR_CONTENTS)}",
                status_code=422,
            )
        exchange: dict[str, Any] = {"actorTokenContent": content}
        scopes = [s for s in obo.get("actor_token_scopes") or [] if s]
        if scopes:
            if content != "M2M":
                raise AppError(
                    "identity.obo_invalid",
                    "actor token scopes apply to actor token content M2M only",
                    status_code=422,
                )
            exchange["actorTokenScopes"] = scopes
        config["tokenExchangeGrantTypeConfig"] = exchange
    return config


def check_obo_idp(
    grant: str,
    *,
    discovery_url: str | None,
    token_endpoint: str | None,
    probe: Any = None,
) -> None:
    """422 `identity.obo_unsupported` when the IdP cannot do the exchange.

    Amazon Cognito user pools accept only authorization_code / refresh_token /
    client_credentials at /oauth2/token (no RFC 8693 or RFC 7523 grant), and
    their discovery document declares no grant_types_supported, so they are
    refused by host (docs/identity.md §8.3). Any other IdP is refused when its
    discovery document declares grant_types_supported without the grant; an IdP
    that declares nothing is let through (the exchange then fails at call time).
    """
    urn = OBO_GRANT_URNS[grant]
    if _is_cognito(discovery_url) or _is_cognito(token_endpoint):
        raise AppError(
            "identity.obo_unsupported",
            "Amazon Cognito user pools do not support OAuth token exchange "
            f"({urn}) — on-behalf-of needs an IdP that implements RFC 8693 or "
            "RFC 7523 (e.g. Okta, Auth0, Keycloak, Microsoft Entra ID); see "
            "docs/identity.md §8.3",
            detail={"required_grant": urn, "idp": "cognito"},
            status_code=422,
        )
    if not discovery_url:
        return
    # the same SSRF-guarded fetch + generic 422 as the inbound JWT probe
    document = inbound_auth.fetch_discovery_document(discovery_url, probe)
    declared = document.get("grant_types_supported")
    if isinstance(declared, list) and urn not in declared:
        raise AppError(
            "identity.obo_unsupported",
            f"the IdP at {discovery_url} does not list {urn} in "
            "grant_types_supported, so it cannot perform the on-behalf-of exchange",
            detail={"required_grant": urn, "grant_types_supported": declared},
            status_code=422,
        )


def _oauth2_config_input(
    vendor: str,
    *,
    client_id: str,
    client_secret: str,
    discovery_url: str | None,
    authorization_endpoint: str | None,
    token_endpoint: str | None,
    issuer: str | None,
    obo: dict[str, Any] | None = None,
) -> dict[str, Any]:
    config_key = VENDOR_CONFIG_KEYS.get(vendor)
    if config_key is None:
        raise AppError(
            "identity.unsupported_vendor",
            f"vendor {vendor!r} is not offered; one of "
            f"{', '.join(sorted(VENDOR_CONFIG_KEYS))}",
            status_code=422,
        )
    if vendor == "CustomOauth2":
        if discovery_url:
            discovery: dict[str, Any] = {"discoveryUrl": discovery_url}
        elif authorization_endpoint and token_endpoint and issuer:
            discovery = {
                "authorizationServerMetadata": {
                    "issuer": issuer,
                    "authorizationEndpoint": authorization_endpoint,
                    "tokenEndpoint": token_endpoint,
                }
            }
        else:
            raise AppError(
                "identity.missing_endpoints",
                "CustomOauth2 requires discovery_url, or all of "
                "authorization_endpoint + token_endpoint + issuer",
                status_code=422,
            )
        custom: dict[str, Any] = {
            "oauthDiscovery": discovery,
            "clientId": client_id,
            "clientSecret": client_secret,
        }
        if obo is not None:
            custom["onBehalfOfTokenExchangeConfig"] = obo_config_input(obo)
        return {config_key: custom}
    if obo is not None:
        raise AppError(
            "identity.obo_vendor_unsupported",
            f"on-behalf-of exchange is configured on CustomOauth2 connections only, "
            f"not {vendor}",
            status_code=422,
        )
    if discovery_url or authorization_endpoint or token_endpoint or issuer:
        raise AppError(
            "identity.endpoints_custom_only",
            f"{vendor} derives its endpoints from the vendor — discovery/endpoint "
            "fields apply to CustomOauth2 only",
            status_code=422,
        )
    return {config_key: {"clientId": client_id, "clientSecret": client_secret}}


def _exists_error(kind: str, name: str) -> AppError:
    return AppError(
        "identity.connection_exists",
        f"a {kind} connection named {name!r} already exists",
        status_code=409,
    )


def _refuse_duplicate(
    control: Any, db: Session, workspace_id: str, kind: str, name: str
) -> None:
    """409 on a name the vault already has — before the secret leaves the request.

    Checked against AWS, not only the ledger: the vault is shared with
    providers created outside Launchpad. A stale audit row for a provider
    deleted out-of-band is dropped so the name can be reused.
    """
    if name in provider_names(control, kind):
        raise _exists_error(kind, name)
    existing = _ledger_row(db, workspace_id, kind, name)
    if existing is not None:
        db.delete(existing)
        db.commit()


def _record(
    db: Session,
    workspace_id: str,
    *,
    name: str,
    kind: str,
    vendor: str,
    created: dict[str, Any],
    client_id: str | None,
    scopes: list[str],
    template: str | None,
    description: str | None,
    created_by: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = IdentityProvider(
        workspace_id=workspace_id,
        name=name,
        kind=kind,
        vendor=vendor,
        arn=created.get("credentialProviderArn", ""),
        callback_url=created.get("callbackUrl") or None,
        client_id=client_id,
        scopes=scopes or None,
        template=template,
        description=description,
        created_by=created_by,
    )
    db.add(row)
    db.commit()
    return _item(
        name=name,
        kind=kind,
        row=row,
        aws={
            "credentialProviderArn": row.arn,
            "credentialProviderVendor": vendor,
            "callbackUrl": row.callback_url,
            # the input shape mirrors the output for the OBO block
            "oauth2ProviderConfigOutput": config or {},
        },
        refs=[],
    )


def create_oauth2_connection(
    control: Any,
    db: Session,
    workspace_id: str,
    *,
    name: str,
    vendor: str,
    client_id: str,
    client_secret: str,
    discovery_url: str | None = None,
    authorization_endpoint: str | None = None,
    token_endpoint: str | None = None,
    issuer: str | None = None,
    scopes: list[str] | None = None,
    template: str | None = None,
    description: str | None = None,
    created_by: str = "",
    obo: dict[str, Any] | None = None,
    probe: Any = None,
) -> dict[str, Any]:
    """Create the OAuth2 provider in the vault and record the audit row.

    The response carries the callback URL: the redirect URI the operator must
    register at the IdP before a 3LO flow can work (M2M does not use it). It is
    re-readable later through `get_connection`.
    """
    config = _oauth2_config_input(
        vendor,
        client_id=client_id,
        client_secret=client_secret,
        discovery_url=discovery_url,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        issuer=issuer,
        obo=obo,
    )
    if obo is not None:
        check_obo_idp(
            config[VENDOR_CONFIG_KEYS[vendor]]["onBehalfOfTokenExchangeConfig"]["grantType"],
            discovery_url=discovery_url,
            token_endpoint=token_endpoint,
            probe=probe,
        )
    _refuse_duplicate(control, db, workspace_id, KIND_OAUTH2, name)
    try:
        created = control.create_oauth2_credential_provider(
            name=name, credentialProviderVendor=vendor, oauth2ProviderConfigInput=config
        )
    except ClientError as exc:
        if _aws_code(exc) == "ConflictException":
            raise _exists_error(KIND_OAUTH2, name) from None
        raise
    return _record(
        db,
        workspace_id,
        name=name,
        kind=KIND_OAUTH2,
        vendor=vendor,
        created=created,
        client_id=client_id,
        scopes=list(scopes or []),
        template=template,
        description=description,
        created_by=created_by,
        config=config,
    )


def create_api_key_connection(
    control: Any,
    db: Session,
    workspace_id: str,
    *,
    name: str,
    api_key: str,
    description: str | None = None,
    created_by: str = "",
) -> dict[str, Any]:
    _refuse_duplicate(control, db, workspace_id, KIND_API_KEY, name)
    try:
        created = control.create_api_key_credential_provider(name=name, apiKey=api_key)
    except ClientError as exc:
        if _aws_code(exc) == "ConflictException":
            raise _exists_error(KIND_API_KEY, name) from None
        raise
    return _record(
        db,
        workspace_id,
        name=name,
        kind=KIND_API_KEY,
        vendor="",
        created=created,
        client_id=None,
        scopes=[],
        template="api_key",
        description=description,
        created_by=created_by,
    )


def delete_connection(
    control: Any,
    db: Session,
    workspace_id: str,
    kind: str,
    name: str,
    *,
    bound_targets: list[dict[str, Any]] | None = None,
) -> None:
    """Delete from the vault + drop the audit row; refuses system, referenced
    and external Connections.

    Only a provider Launchpad created — one with a ledger row in THIS workspace
    — is deleted from the vault. A vault-only provider (``source="external"``
    in the list: created by another tool, another Launchpad, or by hand in the
    same account + region) may back things Launchpad cannot see, so it is
    refused by name rather than deleted. A ledger row whose provider is already
    gone from the vault is still cleaned up.
    """
    _require_kind(kind)
    if name in SYSTEM_PROVIDER_NAMES:
        raise AppError(
            "identity.system_connection",
            f"{name!r} is a system connection managed by bootstrap and cannot be "
            "deleted here",
            status_code=409,
        )
    refs = _references(db, workspace_id, name, bound_targets or [])
    if refs:
        raise AppError(
            "identity.connection_referenced",
            f"connection {name!r} is referenced by {len(refs)} agent(s) or gateway "
            "target(s); detach it first",
            detail={"referenced_by": refs},
            status_code=409,
        )
    row = _ledger_row(db, workspace_id, kind, name)
    known = name in provider_names(control, kind)
    if not known and row is None:
        raise NotFoundError(
            "identity.connection_not_found", f"no {kind} connection named {name!r}"
        )
    if row is None:
        raise AppError(
            "identity.external_connection",
            f"{name!r} was created outside Launchpad — delete it in AgentCore "
            "Identity instead",
            detail={"kind": kind, "name": name},
            status_code=409,
        )
    if known:
        if kind == KIND_OAUTH2:
            control.delete_oauth2_credential_provider(name=name)
        else:
            control.delete_api_key_credential_provider(name=name)
    db.delete(row)
    # the provider and its vaulted tokens are gone: so is every user's grant on it
    oauth_sessions.forget_connection(db, workspace_id, name)
    db.commit()
