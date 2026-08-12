"""Trusted console identity -> Cognito JWT bridge for Gateway Policy.

AgentCore Gateway builds an ``OAuthUser`` principal from the JWT it receives.
``runtimeUserId`` does not become a JWT claim: the existing M2M exchange always
returns the same client principal. Chat therefore needs a real user access token
when Cedar rules must evaluate ``username`` or ``cognito:groups``.

The browser never supplies this token or chooses its subject. Callers pass the
``Identity`` fields already resolved from the signed console session.
"""

import base64
import hashlib
import hmac
import json
import threading
import time
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings, load_yaml_config
from app.core.errors import AppError
from app.services.workspace import WorkspaceContext

ROLE_GROUP = {"admin": "platform-admin", "member": "hr-analyst"}
POLICY_GROUPS = frozenset(ROLE_GROUP.values())
SHADOW_MARKER = "launchpad-policy-shadow-v1"

_token_cache: dict[tuple[str, str, str, str], dict[str, Any]] = {}
_lock = threading.Lock()


def _demo_password(username: str) -> str | None:
    passwords = (load_yaml_config().get("demo_users") or {}).get("passwords") or {}
    value = passwords.get(username)
    return str(value) if value else None


def _shadow_password(username: str) -> str:
    settings = get_settings()
    secret = settings.auth_password.get_secret_value() if settings.auth_password else ""
    if not secret:
        raise AppError(
            "policy.identity_unavailable",
            "Authenticated policy identity requires console authentication",
            status_code=503,
        )
    digest = hmac.new(
        secret.encode("utf-8"),
        f"launchpad-policy-shadow:{username}".encode(),
        hashlib.sha256,
    ).digest()
    # Cognito's pool requires lower/upper/digit. The suffix makes those
    # properties explicit while the HMAC supplies the entropy.
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=") + "aA1"


def _attributes(response: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get("Name")): str(item.get("Value"))
        for item in (response.get("UserAttributes") or response.get("Attributes") or [])
        if item.get("Name") and item.get("Value") is not None
    }


def _ensure_user(
    cognito: Any,
    *,
    pool_id: str,
    username: str,
    email: str | None,
    password: str,
    demo_user: bool,
) -> None:
    try:
        user = cognito.admin_get_user(UserPoolId=pool_id, Username=username)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code != "UserNotFoundException":
            raise
        if demo_user:
            raise AppError(
                "policy.identity_unavailable",
                f"Bootstrapped Cognito user '{username}' is missing; run make bootstrap",
                status_code=503,
            ) from exc
        user = cognito.admin_create_user(
            UserPoolId=pool_id,
            Username=username,
            MessageAction="SUPPRESS",
            UserAttributes=[
                {"Name": "email", "Value": email or f"{username}@launchpad.local"},
                {"Name": "email_verified", "Value": "true"},
                {"Name": "preferred_username", "Value": SHADOW_MARKER},
            ],
        ).get("User") or {}

    attrs = _attributes(user)
    if not demo_user and attrs.get("preferred_username") != SHADOW_MARKER:
        raise AppError(
            "policy.identity_conflict",
            f"Cognito username '{username}' exists but is not owned by Launchpad",
            status_code=409,
        )
    cognito.admin_set_user_password(
        UserPoolId=pool_id,
        Username=username,
        Password=password,
        Permanent=True,
    )


def _reconcile_group(cognito: Any, *, pool_id: str, username: str, role: str) -> str:
    expected = ROLE_GROUP.get(role)
    if expected is None:
        raise AppError(
            "policy.identity_role_invalid",
            f"Console role '{role}' cannot be mapped to a policy principal",
            status_code=500,
        )
    current = {
        str(group.get("GroupName"))
        for group in cognito.admin_list_groups_for_user(
            UserPoolId=pool_id, Username=username, Limit=60
        ).get("Groups", [])
        if group.get("GroupName")
    }
    for group in sorted((current & POLICY_GROUPS) - {expected}):
        cognito.admin_remove_user_from_group(
            UserPoolId=pool_id,
            Username=username,
            GroupName=group,
        )
    if expected not in current:
        cognito.admin_add_user_to_group(
            UserPoolId=pool_id,
            Username=username,
            GroupName=expected,
        )
    return expected


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("access token is not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
    claims = json.loads(decoded)
    if not isinstance(claims, dict):
        raise ValueError("access token payload is not an object")
    return claims


def gateway_user_token(
    workspace: WorkspaceContext, username: str, role: str, email: str | None = None
) -> str | None:
    """Return a verified Cognito user JWT for an authenticated Chat caller.

    ``None`` is deliberate only while the console auth gate is disabled, where
    there is no authenticated human and the established M2M path remains the
    correct identity.
    """

    settings = get_settings()
    if not settings.auth_password or not settings.auth_password.get_secret_value():
        return None
    pool_id = str(workspace.resources.get("user_pool_id") or "")
    client_id = str(workspace.resources.get("user_pool_client_id") or "")
    if not (pool_id and client_id):
        raise AppError(
            "policy.identity_unavailable",
            "Cognito policy identity is not bootstrapped",
            status_code=503,
        )

    # Keyed on the workspace too: each environment has its own user pool, so a
    # token minted against one is rejected by another's gateway.
    key = (workspace.account_id, workspace.region, username, role)
    cached = _token_cache.get(key)
    if cached and float(cached["expires_at"]) > time.time() + 60:
        return str(cached["token"])

    with _lock:
        cached = _token_cache.get(key)
        if cached and float(cached["expires_at"]) > time.time() + 60:
            return str(cached["token"])
        demo_password = _demo_password(username)
        password = demo_password or _shadow_password(username)
        cognito = workspace.client("cognito-idp")
        try:
            _ensure_user(
                cognito,
                pool_id=pool_id,
                username=username,
                email=email,
                password=password,
                demo_user=demo_password is not None,
            )
            expected_group = _reconcile_group(
                cognito,
                pool_id=pool_id,
                username=username,
                role=role,
            )
            auth = cognito.initiate_auth(
                ClientId=client_id,
                AuthFlow="USER_PASSWORD_AUTH",
                AuthParameters={"USERNAME": username, "PASSWORD": password},
            )["AuthenticationResult"]
        except AppError:
            raise
        except (BotoCoreError, ClientError, KeyError) as exc:
            raise AppError(
                "policy.identity_unavailable",
                "Could not obtain the authenticated user's Gateway policy identity",
                status_code=503,
            ) from exc

        token = str(auth.get("AccessToken") or "")
        try:
            claims = _jwt_claims(token)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AppError(
                "policy.identity_invalid",
                "Cognito returned an invalid policy identity token",
                status_code=502,
            ) from exc
        groups = claims.get("cognito:groups") or []
        if claims.get("username") != username or expected_group not in groups:
            raise AppError(
                "policy.identity_invalid",
                "Cognito policy identity does not match the authenticated console user",
                status_code=502,
            )
        expires_at = min(
            float(claims.get("exp") or (time.time() + auth.get("ExpiresIn", 3600))),
            time.time() + float(auth.get("ExpiresIn", 3600)),
        )
        _token_cache[key] = {"token": token, "expires_at": expires_at}
        return token
