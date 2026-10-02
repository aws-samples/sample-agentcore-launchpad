"""The workspace gateway's Consent Portal — as_user (3LO) for Gateway targets.

A Gateway target that acts as the user (``grantType=AUTHORIZATION_CODE``)
needs somewhere for the user to consent: AgentCore's managed Consent Portal
(verified available in us-west-2, 2026-09-28 — see docs/identity.md §7.6). One
portal per gateway; the portal signs the user in with the gateway's inbound IdP
(the Connection named here must issue the same tokens the gateway's JWT
authorizer accepts), then lists the gateway's AUTHORIZATION_CODE targets for
consent. Launchpad never sees the resulting tokens.

The execution role is supplied by the operator, not created here: the platform
mints only per-agent Runtime roles, and the portal role is a workspace-level
trust decision the operator owns (docs/identity.md §7.6 lists what it needs).
Because the backend principal passes that role to AgentCore, the ARN must name
a role in the workspace's own account and partition, and only an administrator
may create or delete the portal (route_policy: ``ADMIN``).
"""

import re
from typing import Any

from botocore.exceptions import ClientError

from app.core.errors import AppError, NotFoundError
from app.services import identity_providers as connections
from app.services.agentcore import consent_portal as portal_api

PORTAL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")
ROLE_ARN_RE = re.compile(
    r"^arn:(?P<partition>aws[a-z-]*):iam::(?P<account>\d{12}):role/[\w+=,.@/-]{1,512}$"
)


def partition_for_region(region: str) -> str:
    """The ARN partition a region belongs to."""
    if region.startswith("cn-"):
        return "aws-cn"
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    if region.startswith("us-iso-"):
        return "aws-iso"
    if region.startswith("us-isob-"):
        return "aws-iso-b"
    return "aws"


def _check_role_arn(execution_role_arn: str, *, account_id: str, region: str) -> None:
    """The portal role must live in this workspace's account and partition —
    never a role the backend principal could pass on another account's behalf."""
    match = ROLE_ARN_RE.match(execution_role_arn)
    if not match:
        raise AppError(
            "identity.invalid_role_arn",
            "the execution role must be an IAM role ARN",
            status_code=422,
        )
    expected_partition = partition_for_region(region)
    if not account_id or match["account"] != account_id or (
        match["partition"] != expected_partition
    ):
        raise AppError(
            "identity.role_account_mismatch",
            "the execution role must be an IAM role in this workspace's own account "
            f"(arn:{expected_partition}:iam::{account_id or '<unknown>'}:role/…)",
            {"account_id": account_id or None, "partition": expected_partition},
            status_code=422,
        )


def _gateway_id(resources: dict[str, Any]) -> str:
    gateway_id = str(resources.get("gateway_id") or "")
    if not gateway_id:
        raise AppError(
            "identity.gateway_missing",
            "this workspace has no gateway — run bootstrap before adding a consent portal",
            status_code=409,
        )
    return gateway_id


def _view(portal: dict[str, Any] | None) -> dict[str, Any] | None:
    if portal is None:
        return None
    idp = portal.get("idpConfig") or {}
    arn = str(idp.get("credentialProviderArn") or "")
    out = {
        "id": portal["consentPortalId"],
        "name": portal["name"],
        "status": portal["status"],
        "status_reason": portal.get("statusReason"),
        "portal_url": portal.get("portalUrl") if portal["status"] == "ACTIVE" else None,
        "execution_role_arn": portal.get("executionRoleArn"),
        "connection": arn.rsplit("/", 1)[-1] if arn else None,
        "scopes": list(idp.get("scopes") or []),
        "audience": idp.get("audience"),
        "created_at": portal.get("createdAt"),
        "updated_at": portal.get("updatedAt"),
    }
    out["callbacks"] = portal_api.callback_urls(out["portal_url"]) if out["portal_url"] else None
    return out


def status(control: Any, resources: dict[str, Any]) -> dict[str, Any]:
    """{gateway_id, portal|None} — the portal read back from AWS every time."""
    gateway_id = str(resources.get("gateway_id") or "")
    if not gateway_id:
        return {"gateway_id": None, "portal": None}
    return {
        "gateway_id": gateway_id,
        "portal": _view(portal_api.portal_for_gateway(control, gateway_id)),
    }


def active_return_url(control: Any, resources: dict[str, Any]) -> str:
    """defaultReturnUrl for an as_user target, or a 409 naming what is missing."""
    gateway_id = _gateway_id(resources)
    portal = portal_api.portal_for_gateway(control, gateway_id)
    if portal is None or portal.get("status") != "ACTIVE" or not portal.get("portalUrl"):
        raise AppError(
            "identity.consent_portal_required",
            "an as_user gateway target needs an ACTIVE consent portal on this "
            "workspace's gateway — create one with POST /api/identity/consent-portal",
            {"portal_status": portal.get("status") if portal else None},
            status_code=409,
        )
    return portal_api.callback_urls(portal["portalUrl"])["target_return"]


def create(
    control: Any,
    resources: dict[str, Any],
    *,
    name: str,
    connection: str,
    scopes: list[str],
    execution_role_arn: str,
    account_id: str,
    region: str,
    audience: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    gateway_id = _gateway_id(resources)
    if not PORTAL_NAME_RE.match(name):
        raise AppError(
            "identity.invalid_portal_name",
            "portal names are letters, digits, '_' and '-' (≤ 50)",
            status_code=422,
        )
    _check_role_arn(execution_role_arn, account_id=account_id, region=region)
    if "openid" not in scopes:
        raise AppError(
            "identity.portal_scopes",
            "the consent portal signs users in with OIDC — its scopes must include 'openid'",
            status_code=422,
        )
    if portal_api.portal_for_gateway(control, gateway_id) is not None:
        raise AppError(
            "identity.consent_portal_exists",
            "this workspace's gateway already has a consent portal",
            status_code=409,
        )
    arn = connections.resolve_arn(control, connections.KIND_OAUTH2, connection)
    try:
        created = portal_api.create_portal(
            control,
            name=name,
            execution_role_arn=execution_role_arn,
            credential_provider_arn=arn,
            scopes=scopes,
            gateway_id=gateway_id,
            audience=audience,
            description=description,
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConflictException":
            raise AppError(
                "identity.consent_portal_exists",
                f"a consent portal named {name!r} already exists",
                status_code=409,
            ) from None
        raise
    return {"gateway_id": gateway_id, "portal": _view(created)}


def delete(control: Any, resources: dict[str, Any]) -> None:
    gateway_id = _gateway_id(resources)
    portal = portal_api.portal_for_gateway(control, gateway_id)
    if portal is None:
        raise NotFoundError("identity.consent_portal_not_found", "no consent portal to delete")
    portal_api.delete_portal(control, portal["consentPortalId"])
