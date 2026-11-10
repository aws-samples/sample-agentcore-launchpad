"""Gateway control-plane wrappers (CreateGateway's siblings) + managed-tag helpers.

Moved in from ``agentcore/policy.py`` when the AWS Policy Engine was removed
(N1 Task 3). No gateway-invoke transport lives here — see
``templates/sigv4_gateway_transport.py.tmpl`` (generated code) and
``services/mcp_client.py`` (console-side).
"""

import time
from collections.abc import Callable
from typing import Any

GATEWAY_FAILURES = {"FAILED", "UPDATE_UNSUCCESSFUL"}

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


def synchronize_gateway_target(client: Any, gateway_id: str, target_id: str) -> dict[str, Any]:
    """SynchronizeGatewayTargets for exactly one target.

    The API takes ``targetIdList`` (fixed length 1 in the pinned model) and answers
    ``202 {targets: [GatewayTarget]}``; the caller gets that single target back.
    Pending-auth and static-``mcpToolSchema`` targets are refused by the service
    with ``ConflictException`` — the governance layer gates those before calling.
    """
    response = client.synchronize_gateway_targets(
        gatewayIdentifier=gateway_id,
        targetIdList=[target_id],
    )
    targets = response.get("targets") or []
    return targets[0] if targets else {}


def list_gateway_rate_limits(client: Any, gateway_id: str) -> list[dict[str, Any]]:
    """Every rate limit on a Gateway, following ``nextToken`` to the end."""
    return _paginate(
        client,
        "list_gateway_rate_limits",
        "rateLimits",
        gatewayIdentifier=gateway_id,
    )


def get_gateway_rate_limit(client: Any, gateway_id: str, rate_limit_id: str) -> dict[str, Any]:
    return client.get_gateway_rate_limit(
        gatewayIdentifier=gateway_id,
        rateLimitId=rate_limit_id,
    )


def create_gateway_rate_limit(
    client: Any,
    *,
    gateway_id: str,
    dimension_keys: list[str],
    entries: list[dict[str, Any]],
    description: str | None = None,
    client_token: str | None = None,
) -> dict[str, Any]:
    """CreateGatewayRateLimit with exactly the validated payload.

    ``dimensionKeys`` are fixed for the lifetime of the rate limit; only
    ``entries`` and ``description`` can change afterwards. The service raises
    ``ConflictException`` when the Gateway already has a rate limit with the same
    dimension-key set, or while the Gateway is busy.
    """
    params: dict[str, Any] = {
        "gatewayIdentifier": gateway_id,
        "dimensionKeys": list(dimension_keys),
        "entries": entries,
    }
    if description is not None:
        params["description"] = description
    if client_token:
        params["clientToken"] = _client_token(client_token)
    return client.create_gateway_rate_limit(**params)


def update_gateway_rate_limit(
    client: Any,
    *,
    gateway_id: str,
    rate_limit_id: str,
    entries: list[dict[str, Any]],
    description: str | None = None,
) -> dict[str, Any]:
    """UpdateGatewayRateLimit — ``entries`` use replace semantics."""
    params: dict[str, Any] = {
        "gatewayIdentifier": gateway_id,
        "rateLimitId": rate_limit_id,
        "entries": entries,
    }
    if description is not None:
        params["description"] = description
    return client.update_gateway_rate_limit(**params)


def delete_gateway_rate_limit(
    client: Any, *, gateway_id: str, rate_limit_id: str
) -> dict[str, Any]:
    return client.delete_gateway_rate_limit(
        gatewayIdentifier=gateway_id,
        rateLimitId=rate_limit_id,
    )


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
