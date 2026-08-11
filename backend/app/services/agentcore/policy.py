"""AgentCore Gateway and Policy control-plane wrappers.

The Policy and Gateway APIs are preview surfaces. This module owns their
request/response shapes so product services can work with stable primitives and
tests can inject plain client stubs.
"""

import time
from collections.abc import Callable
from typing import Any

GATEWAY_FAILURES = {"FAILED", "UPDATE_UNSUCCESSFUL"}
POLICY_FAILURES = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}
POLICY_ENGINE_FAILURES = POLICY_FAILURES
GENERATION_FAILURES = {"GENERATE_FAILED", "DELETE_FAILED"}

MANAGED_TAG = "agentcore-launchpad:managed"
MANAGED_BY_TAG = "agentcore-launchpad:managed-by"
MANAGED_TAGS = {
    MANAGED_TAG: "true",
    MANAGED_BY_TAG: "agentcore-launchpad",
}
_CLIENT_TOKEN_MIN_LENGTH = 33


def _client_token(value: str) -> str:
    if len(value) >= _CLIENT_TOKEN_MIN_LENGTH:
        return value
    return f"launchpad-{value}"

_GATEWAY_UPDATE_FIELDS = (
    "description",
    "protocolType",
    "protocolConfiguration",
    "authorizerConfiguration",
    "kmsKeyArn",
    "customTransformConfiguration",
    "interceptorConfigurations",
    "exceptionLevel",
    "wafConfiguration",
)


def _paginate(
    client: Any,
    operation: str,
    result_key: str,
    *,
    max_results: int = 100,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    token: str | None = None
    method = getattr(client, operation)
    while True:
        params = {**kwargs, "maxResults": max_results}
        if token:
            params["nextToken"] = token
        response = method(**params)
        items.extend(response.get(result_key) or [])
        token = response.get("nextToken")
        if not token:
            return items


def list_gateways(client: Any) -> list[dict[str, Any]]:
    return _paginate(client, "list_gateways", "items")


def get_gateway(client: Any, gateway_id: str) -> dict[str, Any]:
    return client.get_gateway(gatewayIdentifier=gateway_id)


def list_gateway_targets(client: Any, gateway_id: str) -> list[dict[str, Any]]:
    return _paginate(
        client,
        "list_gateway_targets",
        "items",
        gatewayIdentifier=gateway_id,
    )


def get_gateway_target(
    client: Any,
    gateway_id: str,
    target_id: str,
) -> dict[str, Any]:
    return client.get_gateway_target(
        gatewayIdentifier=gateway_id,
        targetId=target_id,
    )


def list_gateway_target_details(client: Any, gateway_id: str) -> list[dict[str, Any]]:
    return [
        get_gateway_target(client, gateway_id, summary["targetId"])
        for summary in list_gateway_targets(client, gateway_id)
    ]


def list_tags(client: Any, resource_arn: str) -> dict[str, str]:
    return dict(client.list_tags_for_resource(resourceArn=resource_arn).get("tags") or {})


def tag_managed(client: Any, resource_arn: str) -> None:
    client.tag_resource(resourceArn=resource_arn, tags=MANAGED_TAGS)


def untag_managed(client: Any, resource_arn: str) -> None:
    client.untag_resource(resourceArn=resource_arn, tagKeys=list(MANAGED_TAGS))


def is_managed(tags: dict[str, str]) -> bool:
    return (
        tags.get(MANAGED_TAG) == "true"
        and tags.get(MANAGED_BY_TAG) == "agentcore-launchpad"
    )


def list_policy_engines(client: Any) -> list[dict[str, Any]]:
    return _paginate(client, "list_policy_engines", "policyEngines")


def get_policy_engine(client: Any, engine_id: str) -> dict[str, Any]:
    return client.get_policy_engine(policyEngineId=engine_id)


def create_policy_engine(
    client: Any,
    *,
    name: str,
    description: str | None = None,
    client_token: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name, "tags": MANAGED_TAGS}
    if description:
        params["description"] = description
    if client_token:
        params["clientToken"] = _client_token(client_token)
    return client.create_policy_engine(**params)


def list_policies(client: Any, engine_id: str) -> list[dict[str, Any]]:
    return _paginate(
        client,
        "list_policies",
        "policies",
        policyEngineId=engine_id,
    )


def get_policy(client: Any, engine_id: str, policy_id: str) -> dict[str, Any]:
    return client.get_policy(policyEngineId=engine_id, policyId=policy_id)


def create_policy(
    client: Any,
    *,
    engine_id: str,
    name: str,
    statement: str,
    description: str | None = None,
    client_token: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "policyEngineId": engine_id,
        "name": name,
        "definition": {"cedar": {"statement": statement}},
        "validationMode": "FAIL_ON_ANY_FINDINGS",
        "enforcementMode": "LOG_ONLY",
    }
    if description:
        params["description"] = description
    if client_token:
        params["clientToken"] = _client_token(client_token)
    return client.create_policy(**params)


def update_policy(
    client: Any,
    *,
    engine_id: str,
    policy_id: str,
    statement: str | None = None,
    enforcement_mode: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "policyEngineId": engine_id,
        "policyId": policy_id,
        "validationMode": "FAIL_ON_ANY_FINDINGS",
    }
    if statement is not None:
        params["definition"] = {"cedar": {"statement": statement}}
    if enforcement_mode is not None:
        params["enforcementMode"] = enforcement_mode
    if description is not None:
        params["description"] = {"optionalValue": description}
    return client.update_policy(**params)


def delete_policy(client: Any, *, engine_id: str, policy_id: str) -> dict[str, Any]:
    return client.delete_policy(policyEngineId=engine_id, policyId=policy_id)


def gateway_update_params(
    gateway: dict[str, Any],
    policy_engine_configuration: dict[str, str],
) -> dict[str, Any]:
    """Rebuild UpdateGateway from a fresh GetGateway response.

    UpdateGateway uses replace semantics. Every supported mutable field is
    echoed and only ``policyEngineConfiguration`` is changed.
    """
    required = ("gatewayId", "name", "roleArn", "authorizerType")
    missing = [key for key in required if not gateway.get(key)]
    if missing:
        raise ValueError(f"gateway response missing required update fields: {', '.join(missing)}")
    params: dict[str, Any] = {
        "gatewayIdentifier": gateway["gatewayId"],
        "name": gateway["name"],
        "roleArn": gateway["roleArn"],
        "authorizerType": gateway["authorizerType"],
        "policyEngineConfiguration": dict(policy_engine_configuration),
    }
    for field in _GATEWAY_UPDATE_FIELDS:
        if gateway.get(field) is not None:
            params[field] = gateway[field]
    return params


def update_gateway_policy_configuration(
    client: Any,
    *,
    gateway_id: str,
    engine_arn: str,
    mode: str,
) -> dict[str, Any]:
    gateway = get_gateway(client, gateway_id)
    return client.update_gateway(
        **gateway_update_params(gateway, {"arn": engine_arn, "mode": mode})
    )


def start_policy_generation(
    client: Any,
    *,
    engine_id: str,
    gateway_arn: str,
    name: str,
    text: str,
    client_token: str | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "policyEngineId": engine_id,
        "resource": {"arn": gateway_arn},
        "content": {"rawText": text},
        "name": name,
    }
    if client_token:
        params["clientToken"] = _client_token(client_token)
    return client.start_policy_generation(**params)


def get_policy_generation(
    client: Any,
    engine_id: str,
    generation_id: str,
) -> dict[str, Any]:
    return client.get_policy_generation(
        policyEngineId=engine_id,
        policyGenerationId=generation_id,
    )


def list_policy_generation_assets(
    client: Any,
    engine_id: str,
    generation_id: str,
) -> list[dict[str, Any]]:
    return _paginate(
        client,
        "list_policy_generation_assets",
        "policyGenerationAssets",
        policyEngineId=engine_id,
        policyGenerationId=generation_id,
    )


def _wait(
    getter: Callable[[], dict[str, Any]],
    *,
    ready: set[str],
    failures: set[str],
    label: str,
    timeout_s: int,
    interval_s: int,
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        detail = getter()
        status = str(detail.get("status") or "")
        if status in ready:
            return detail
        if status in failures:
            reasons = detail.get("statusReasons") or []
            raise RuntimeError(f"{label} entered {status}: {reasons}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"{label} still {status} after {timeout_s}s")
        sleeper(interval_s)


def wait_gateway_ready(
    client: Any,
    gateway_id: str,
    *,
    timeout_s: int = 600,
    interval_s: int = 5,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    return _wait(
        lambda: get_gateway(client, gateway_id),
        ready={"READY"},
        failures=GATEWAY_FAILURES,
        label=f"gateway {gateway_id}",
        timeout_s=timeout_s,
        interval_s=interval_s,
        sleeper=sleeper,
    )


def wait_policy_engine_active(
    client: Any,
    engine_id: str,
    *,
    timeout_s: int = 600,
    interval_s: int = 5,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    return _wait(
        lambda: get_policy_engine(client, engine_id),
        ready={"ACTIVE"},
        failures=POLICY_ENGINE_FAILURES,
        label=f"policy engine {engine_id}",
        timeout_s=timeout_s,
        interval_s=interval_s,
        sleeper=sleeper,
    )


def wait_policy_active(
    client: Any,
    engine_id: str,
    policy_id: str,
    *,
    timeout_s: int = 600,
    interval_s: int = 5,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    return _wait(
        lambda: get_policy(client, engine_id, policy_id),
        ready={"ACTIVE"},
        failures=POLICY_FAILURES,
        label=f"policy {policy_id}",
        timeout_s=timeout_s,
        interval_s=interval_s,
        sleeper=sleeper,
    )


def wait_policy_deleted(
    client: Any,
    engine_id: str,
    policy_id: str,
    *,
    timeout_s: int = 600,
    interval_s: int = 5,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            detail = get_policy(client, engine_id, policy_id)
        except client.exceptions.ResourceNotFoundException:
            return
        status = str(detail.get("status") or "")
        if status == "DELETE_FAILED":
            reasons = detail.get("statusReasons") or []
            raise RuntimeError(f"policy {policy_id} entered {status}: {reasons}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"policy {policy_id} still {status} after {timeout_s}s")
        sleeper(interval_s)
