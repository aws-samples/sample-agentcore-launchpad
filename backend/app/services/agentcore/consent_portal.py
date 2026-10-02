"""AgentCore Identity Consent Portal wrappers (``bedrock-agentcore-control``).

A Consent Portal is the service-hosted page where a user signs in with the
gateway's inbound IdP and authorizes the gateway's ``AUTHORIZATION_CODE``
targets — the Gateway-side counterpart of the Runtime ``/auth/return`` leg.
The operations are recent (botocore ≥ 1.43.103, verified against the service
model 2026-09-28): Create/Update answer 202 and the portal walks
``CREATING → ACTIVE``; ``portalUrl`` is only meaningful once ACTIVE.

Preview-API drift stays in this module: callers get plain dicts with the
fields below, never the raw response shape.
"""

from typing import Any

SOURCE_GATEWAY = "agentcore-gateway"
_FIELDS = (
    "consentPortalId",
    "consentPortalArn",
    "name",
    "description",
    "status",
    "statusReason",
    "portalUrl",
    "executionRoleArn",
    "idpConfig",
    "sources",
    "createdAt",
    "updatedAt",
)


def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
    out = {key: raw.get(key) for key in _FIELDS}
    for key in ("createdAt", "updatedAt"):
        value = out[key]
        out[key] = value.isoformat() if hasattr(value, "isoformat") else value
    out["sources"] = list(out["sources"] or [])
    return out


def list_portals(control: Any) -> list[dict[str, Any]]:
    portals: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"maxResults": 100}
        if token:
            kwargs["nextToken"] = token
        page = control.list_consent_portals(**kwargs)
        portals.extend(_normalize(p) for p in page.get("consentPortals") or [])
        token = page.get("nextToken")
        if not token:
            return portals


def get_portal(control: Any, portal_id: str) -> dict[str, Any]:
    return _normalize(control.get_consent_portal(consentPortalIdentifier=portal_id))


def portal_for_gateway(control: Any, gateway_id: str) -> dict[str, Any] | None:
    """The portal whose source is this gateway (the service allows exactly one
    source per portal). The summary omits ``idpConfig``, so the match is read
    back in full."""
    for summary in list_portals(control):
        if any(
            s.get("type") == SOURCE_GATEWAY and s.get("identifier") == gateway_id
            for s in summary["sources"]
        ):
            return get_portal(control, summary["consentPortalId"])
    return None


def create_portal(
    control: Any,
    *,
    name: str,
    execution_role_arn: str,
    credential_provider_arn: str,
    scopes: list[str],
    gateway_id: str,
    audience: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    idp: dict[str, Any] = {"credentialProviderArn": credential_provider_arn, "scopes": scopes}
    if audience:
        idp["audience"] = audience
    kwargs: dict[str, Any] = {
        "name": name,
        "executionRoleArn": execution_role_arn,
        "idpConfig": idp,
        "sources": [{"identifier": gateway_id, "type": SOURCE_GATEWAY}],
    }
    if description:
        kwargs["description"] = description
    return _normalize(control.create_consent_portal(**kwargs))


def delete_portal(control: Any, portal_id: str) -> None:
    control.delete_consent_portal(consentPortalIdentifier=portal_id)


def callback_urls(portal_url: str) -> dict[str, str]:
    """The two URLs an operator must wire (devguide, consent-portal setup):
    ``idp_callback`` goes on the inbound IdP app client's allowed callbacks;
    ``target_return`` is every AUTHORIZATION_CODE target's defaultReturnUrl."""
    base = portal_url.rstrip("/")
    return {"idp_callback": f"{base}/callback", "target_return": f"{base}/connect/callback"}
