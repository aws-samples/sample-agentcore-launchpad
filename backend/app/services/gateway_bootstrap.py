"""Gateway bootstrap — extends the phase-2 singletons with the Build-Tools layer.

ensure_* functions are create-if-missing by name (idempotent), same contract
as ensure_registry / ensure_memory in bootstrap.py.
"""

import json
import time
from pathlib import Path
from typing import Any

import yaml

from app.core.config import REPO_ROOT

GATEWAY_NAME = "launchpad-gw"
HR_TARGET_NAME = "hr-database"
FACTS_TARGET_NAME = "office-facts"
API_KEY_PROVIDER_NAME = "launchpad-office-facts-key"

SAMPLES = REPO_ROOT / "samples"


def ensure_gateway(
    control: Any,
    *,
    role_arn: str,
    user_pool_id: str,
    client_id: str,
    region: str,
    name: str = GATEWAY_NAME,
    timeout_s: int = 300,
) -> tuple[dict[str, str], bool]:
    """Create (or reuse) the shared MCP gateway with Cognito-JWT inbound auth."""
    items: list[dict[str, Any]] = []
    token = None
    while True:
        kwargs = {"maxResults": 100} | ({"nextToken": token} if token else {})
        page = control.list_gateways(**kwargs)
        items.extend(page.get("items", []))
        token = page.get("nextToken")
        if not token:
            break
    for gw in items:
        if gw.get("name") == name:
            detail = control.get_gateway(gatewayIdentifier=gw["gatewayId"])
            return (
                {
                    "id": detail["gatewayId"],
                    "arn": detail["gatewayArn"],
                    "url": detail["gatewayUrl"],
                },
                False,
            )

    discovery = (
        f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        "/.well-known/openid-configuration"
    )
    created = control.create_gateway(
        name=name,
        description="AgentCore Launchpad shared MCP gateway",
        roleArn=role_arn,
        protocolType="MCP",
        authorizerType="CUSTOM_JWT",
        authorizerConfiguration={
            "customJWTAuthorizer": {
                "discoveryUrl": discovery,
                "allowedClients": [client_id],
            }
        },
        exceptionLevel="DEBUG",
    )
    gateway_id = created["gatewayId"]
    _wait_gateway_ready(control, gateway_id, timeout_s=timeout_s)
    detail = control.get_gateway(gatewayIdentifier=gateway_id)
    return (
        {"id": gateway_id, "arn": detail["gatewayArn"], "url": detail["gatewayUrl"]},
        True,
    )


def _wait_gateway_ready(control: Any, gateway_id: str, timeout_s: int = 300) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = control.get_gateway(gatewayIdentifier=gateway_id)["status"]
        if status == "READY":
            return
        if status == "FAILED":
            raise RuntimeError(f"gateway {gateway_id} FAILED")
        # Deliberate polling interval; the surrounding loop owns the deadline.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    raise TimeoutError(f"gateway {gateway_id} not READY after {timeout_s}s")


def ensure_api_key_provider(
    control: Any, api_key: str, name: str = API_KEY_PROVIDER_NAME
) -> tuple[str, bool]:
    """Store the sample REST API key in AgentCore Identity (create-if-missing)."""
    providers = control.list_api_key_credential_providers(maxResults=100).get(
        "credentialProviders", []
    )
    for provider in providers:
        if provider.get("name") == name:
            return provider["credentialProviderArn"], False
    created = control.create_api_key_credential_provider(name=name, apiKey=api_key)
    return created["credentialProviderArn"], True

GATEWAY_M2M_PROVIDER_NAME = "launchpad-gw-m2m"
GATEWAY_SCOPE = "launchpad-gw/invoke"


def ensure_oauth_provider(
    control: Any,
    cognito_client: Any,
    *,
    user_pool_id: str,
    m2m_client_id: str,
    region: str,
    name: str = GATEWAY_M2M_PROVIDER_NAME,
) -> tuple[str, bool]:
    """OAuth2 credential provider (client_credentials against Cognito) that
    harnesses use for outbound gateway auth."""
    providers = control.list_oauth2_credential_providers(maxResults=20).get(
        "credentialProviders", []
    )
    for provider in providers:
        if provider.get("name") == name:
            return provider["credentialProviderArn"], False
    secret = cognito_client.describe_user_pool_client(
        UserPoolId=user_pool_id, ClientId=m2m_client_id
    )["UserPoolClient"]["ClientSecret"]
    discovery = (
        f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
        "/.well-known/openid-configuration"
    )
    created = control.create_oauth2_credential_provider(
        name=name,
        credentialProviderVendor="CustomOauth2",
        oauth2ProviderConfigInput={
            "customOauth2ProviderConfig": {
                "oauthDiscovery": {"discoveryUrl": discovery},
                "clientId": m2m_client_id,
                "clientSecret": secret,
            }
        },
    )
    return created["credentialProviderArn"], True


def ensure_gateway_allows_client(
    control: Any, gateway_id: str, client_id: str, timeout_s: int = 300
) -> bool:
    """Append client_id to the gateway's allowedClients if missing. Returns changed."""
    gateway = control.get_gateway(gatewayIdentifier=gateway_id)
    auth = gateway["authorizerConfiguration"]
    allowed = auth.get("customJWTAuthorizer", {}).get("allowedClients", [])
    if client_id in allowed:
        return False
    auth["customJWTAuthorizer"]["allowedClients"] = allowed + [client_id]
    control.update_gateway(
        gatewayIdentifier=gateway_id,
        name=gateway["name"],
        roleArn=gateway["roleArn"],
        protocolType=gateway.get("protocolType", "MCP"),
        authorizerType=gateway["authorizerType"],
        authorizerConfiguration=auth,
    )
    _wait_gateway_ready(control, gateway_id, timeout_s=timeout_s)
    return True


def _list_targets(control: Any, gateway_id: str) -> dict[str, dict[str, Any]]:
    items = control.list_gateway_targets(gatewayIdentifier=gateway_id, maxResults=100).get(
        "items", []
    )
    return {t["name"]: t for t in items}


def ensure_lambda_target(
    control: Any, gateway_id: str, lambda_arn: str
) -> tuple[str, bool]:
    existing = _list_targets(control, gateway_id)
    if HR_TARGET_NAME in existing:
        return existing[HR_TARGET_NAME]["targetId"], False
    tools = json.loads((SAMPLES / "hr_database_lambda" / "tool_schema.json").read_text())
    created = control.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=HR_TARGET_NAME,
        description="HR database Lambda → MCP tools",
        targetConfiguration={
            "mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": tools}}}
        },
        credentialProviderConfigurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}
        ],
    )
    _wait_target_ready(control, gateway_id, created["targetId"])
    return created["targetId"], True


def ensure_openapi_target(
    control: Any, gateway_id: str, api_url: str, provider_arn: str
) -> tuple[str, bool]:
    existing = _list_targets(control, gateway_id)
    if FACTS_TARGET_NAME in existing:
        return existing[FACTS_TARGET_NAME]["targetId"], False
    spec = yaml.safe_load((SAMPLES / "rest_api" / "openapi.yaml").read_text())
    spec["servers"] = [{"url": api_url.rstrip("/")}]
    created = control.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=FACTS_TARGET_NAME,
        description="office-facts REST API (OpenAPI) → MCP tools",
        targetConfiguration={"mcp": {"openApiSchema": {"inlinePayload": json.dumps(spec)}}},
        credentialProviderConfigurations=[
            {
                "credentialProviderType": "API_KEY",
                "credentialProvider": {
                    "apiKeyCredentialProvider": {
                        "providerArn": provider_arn,
                        "credentialLocation": "HEADER",
                        "credentialParameterName": "x-api-key",
                    }
                },
            }
        ],
    )
    _wait_target_ready(control, gateway_id, created["targetId"])
    return created["targetId"], True


def _wait_target_ready(
    control: Any, gateway_id: str, target_id: str, timeout_s: int = 300
) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = control.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id
        )["status"]
        if status == "READY":
            return
        if status in ("FAILED", "CREATE_FAILED"):
            detail = control.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            )
            raise RuntimeError(
                f"target {target_id} {status}: {detail.get('statusReasons')}"
            )
        # Deliberate polling interval; the surrounding loop owns the deadline.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    raise TimeoutError(f"target {target_id} not READY after {timeout_s}s")


def run_gateway_bootstrap(
    control: Any, apigw_client: Any, config: dict[str, Any], cognito_client: Any = None
) -> dict:
    """Idempotently ensure gateway + the demo targets + M2M outbound auth.

    The Build-Tools demo layer (hr-database Lambda, office-facts REST API) is
    CDK-shipped sample code, so it only exists where the stack is deployed. Its
    identifiers are read defensively: a gateway with no demo targets is a valid
    environment, and `workspace_bootstrap` provisions exactly that.
    """
    resources = config.get("resources", {})
    region = config.get("region", "us-west-2")
    api_key_id = resources.get("office_facts_api_key_id") or ""
    hr_lambda_arn = resources.get("hr_lambda_arn") or ""
    facts_api_url = resources.get("office_facts_api_url") or ""

    provider_arn, provider_created = "", False
    if api_key_id:
        api_key_value = apigw_client.get_api_key(apiKey=api_key_id, includeValue=True)["value"]
        provider_arn, provider_created = ensure_api_key_provider(control, api_key_value)
    gateway, gateway_created = ensure_gateway(
        control,
        role_arn=resources["gateway_role_arn"],
        user_pool_id=resources["user_pool_id"],
        client_id=resources["user_pool_client_id"],
        region=region,
    )
    hr_target, hr_created = "", False
    if hr_lambda_arn:
        hr_target, hr_created = ensure_lambda_target(control, gateway["id"], hr_lambda_arn)
    facts_target, facts_created = "", False
    if facts_api_url and provider_arn:
        facts_target, facts_created = ensure_openapi_target(
            control, gateway["id"], facts_api_url, provider_arn
        )

    oauth_arn, oauth_created, m2m_allowed = "", False, False
    m2m_client_id = resources.get("m2m_client_id", "")
    if cognito_client is not None and m2m_client_id:
        oauth_arn, oauth_created = ensure_oauth_provider(
            control,
            cognito_client,
            user_pool_id=resources["user_pool_id"],
            m2m_client_id=m2m_client_id,
            region=region,
        )
        m2m_allowed = ensure_gateway_allows_client(control, gateway["id"], m2m_client_id)

    return {
        "gateway": {**gateway, "created": gateway_created},
        "api_key_provider": {"arn": provider_arn, "created": provider_created},
        "oauth_provider": {
            "arn": oauth_arn, "created": oauth_created, "allowed_added": m2m_allowed,
        },
        "targets": {
            HR_TARGET_NAME: {"id": hr_target, "created": hr_created},
            FACTS_TARGET_NAME: {"id": facts_target, "created": facts_created},
        },
    }


def load_config_file() -> dict[str, Any]:
    path = Path(REPO_ROOT / "config" / "launchpad.yaml")
    return yaml.safe_load(path.read_text()) if path.is_file() else {}
