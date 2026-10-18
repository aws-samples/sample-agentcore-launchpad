"""Gateways other than launchpad-gw that may receive the signed-in user's token.

The logged-in chat path swaps launchpad-gw for a ``remote_mcp`` server carrying the
user's console access token, so Gateway targets acting *as user* (3LO) and Cedar
policies see that human. A second Gateway qualifies for the same swap only when it
already admits exactly these tokens on its own: its CUSTOM_JWT authorizer trusts the
workspace user pool, allow-lists both the console client (the user token) and the
workspace M2M client (the deploy-time CLIENT_CREDENTIALS identity), and no
interceptor is configured to receive request headers. The last condition matters:
forwarding a bearer to a Gateway whose interceptor reads headers would hand the
user's token to code the platform does not own.

Nothing here widens what a Gateway accepts; it only stops the platform from refusing
to mount a Gateway that is already wired to the same identity as launchpad-gw.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def workspace_discovery_url(resources: Mapping[str, Any]) -> str | None:
    """The OIDC discovery URL of the workspace user pool, or None when unknown."""
    pool_id = str(resources.get("user_pool_id") or "")
    region, sep, _ = pool_id.partition("_")
    if not sep or not region:
        return None
    return (
        f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
        "/.well-known/openid-configuration"
    )


def _passes_headers_to_interceptor(gateway: Mapping[str, Any]) -> bool:
    for interceptor in gateway.get("interceptorConfigurations") or []:
        if not isinstance(interceptor, Mapping):
            return True  # unreadable shape: fail closed
        config = interceptor.get("inputConfiguration") or {}
        if not isinstance(config, Mapping) or config.get("passRequestHeaders"):
            return True
    return False


def trusts_workspace_users(gateway: Mapping[str, Any], resources: Mapping[str, Any]) -> bool:
    """Whether ``gateway`` is a non-launchpad-gw Gateway eligible for the user token."""
    if gateway.get("authorizerType") != "CUSTOM_JWT" or gateway.get("protocolType") != "MCP":
        return False
    if gateway.get("gatewayId") and gateway.get("gatewayId") == resources.get("gateway_id"):
        return False  # launchpad-gw keeps its own, older path
    console_client = str(resources.get("user_pool_client_id") or "")
    m2m_client = str(resources.get("m2m_client_id") or "")
    discovery = workspace_discovery_url(resources)
    if not (console_client and m2m_client and discovery and resources.get("oauth_provider_arn")):
        return False
    jwt = (gateway.get("authorizerConfiguration") or {}).get("customJWTAuthorizer") or {}
    if jwt.get("discoveryUrl") != discovery:
        return False
    # Cognito access tokens carry client_id and no aud: an audience rule would reject
    # the user token, so such a Gateway is not wired for it.
    if jwt.get("allowedAudience"):
        return False
    allowed = set(jwt.get("allowedClients") or [])
    if not {console_client, m2m_client} <= allowed:
        return False
    return not _passes_headers_to_interceptor(gateway)
