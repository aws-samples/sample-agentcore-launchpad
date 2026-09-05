"""Thin wrappers over the Harness control/data APIs.

Every function takes an explicit client so tests inject stubs that capture
kwargs. Payload shapes follow bedrock-agentcore-control 1.43.x.
"""

import time
import uuid
from collections.abc import Mapping
from typing import Any

TERMINAL_FAILURES = {"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}
BUILTIN_TOOL_TYPES = {
    "code-interpreter": "agentcore_code_interpreter",
    "browser": "agentcore_browser",
}


def create_harness(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """CreateHarness; returns the harness detail dict (harnessId, arn, status…)."""
    return client.create_harness(**params)["harness"]


def update_harness(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """UpdateHarness — publishes a new harness version in place. Same harnessId
    and ARN; ``params`` carries ``harnessId`` plus the edited config (model,
    systemPrompt, tools, memory…), i.e. ``wrap_params_for_update`` output."""
    return client.update_harness(**params)["harness"]


def wrap_params_for_update(params: dict[str, Any]) -> dict[str, Any]:
    """Create-style params → UpdateHarness kwargs (same pattern as the registry's
    ``wrap_descriptors_for_update``). Update reuses the create shapes except
    ``memory``, whose value must sit in {"optionalValue": …}. Omitting memory
    means "keep the old config", so a spec without memory sends the explicit
    ``disabled`` variant to detach it. ``tools``/``skills`` share that omit=keep
    semantic — send explicit empty lists so deselecting the last tool (e.g. the
    only mounted KB) actually detaches it. Drops the immutable ``harnessName``."""
    update = {k: v for k, v in params.items() if k != "harnessName"}
    update["memory"] = {"optionalValue": update.get("memory") or {"disabled": {}}}
    update.setdefault("tools", [])
    update.setdefault("skills", [])
    return update


def get_harness(client: Any, harness_id: str) -> dict[str, Any]:
    return client.get_harness(harnessId=harness_id)["harness"]


def list_harnesses(client: Any) -> list[dict[str, Any]]:
    """Return every Harness summary across all ListHarnesses pages."""
    harnesses: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"maxResults": 100}
    while True:
        page = client.list_harnesses(**kwargs)
        harnesses.extend(page.get("harnesses", []))
        token = page.get("nextToken")
        if not token:
            return harnesses
        kwargs["nextToken"] = token


def list_harness_versions(client: Any, harness_id: str) -> list[dict[str, Any]]:
    """Every version of one harness across all ListHarnessVersions pages.
    ``HarnessVersionSummary`` has no ``description`` and uses ``updatedAt`` (not
    ``lastUpdatedAt``): {harnessVersion, status, createdAt, updatedAt,
    failureReason, …}. The caller projects."""
    versions: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"harnessId": harness_id, "maxResults": 100}
    while True:
        page = client.list_harness_versions(**kwargs)
        versions.extend(page.get("harnessVersions", []))
        token = page.get("nextToken")
        if not token:
            return versions
        kwargs["nextToken"] = token


def list_harness_endpoints(client: Any, harness_id: str) -> list[dict[str, Any]]:
    """Every endpoint of one harness across all ListHarnessEndpoints pages:
    {endpointName, liveVersion, targetVersion, status, description, createdAt,
    updatedAt, failureReason, …}."""
    endpoints: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"harnessId": harness_id, "maxResults": 100}
    while True:
        page = client.list_harness_endpoints(**kwargs)
        endpoints.extend(page.get("endpoints", []))
        token = page.get("nextToken")
        if not token:
            return endpoints
        kwargs["nextToken"] = token


def delete_harness(client: Any, harness_id: str) -> None:
    client.delete_harness(harnessId=harness_id)


def wait_harness_ready(
    client: Any,
    harness_id: str,
    timeout_s: int = 300,
    interval_s: int = 5,
    sleeper: Any = time.sleep,
) -> dict[str, Any]:
    """Poll GetHarness until READY; raise on terminal failure or timeout."""
    deadline = time.monotonic() + timeout_s
    while True:
        harness = get_harness(client, harness_id)
        status = harness["status"]
        if status == "READY":
            return harness
        if status in TERMINAL_FAILURES:
            reason = harness.get("failureReason", "no failureReason provided")
            raise RuntimeError(f"harness {harness_id} entered {status}: {reason}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"harness {harness_id} still {status} after {timeout_s}s")
        sleeper(interval_s)


def new_session_id() -> str:
    # Runtime session ids must be long (≥33 chars); two uuid4 hex = 64.
    return uuid.uuid4().hex + uuid.uuid4().hex


def user_authenticated_tools(
    spec: Mapping[str, Any],
    resources: Mapping[str, Any],
    access_token: str,
) -> list[dict[str, Any]]:
    """Harness invocation tools with launchpad-gw authenticated as one user.

    ``InvokeHarness.tools`` replaces the configured tool list for that request,
    so every non-user tool declared by the stored spec is reconstructed too.
    The user token is accepted only for the bootstrapped shared Gateway whose
    Cognito authorizer issued it.
    """

    result: list[dict[str, Any]] = []
    attached_user_gateway = False
    configured_gateway_id = str(resources.get("gateway_id") or "")
    for raw in spec.get("tools") or []:
        if not isinstance(raw, Mapping):
            continue
        tool_type = str(raw.get("type") or "")
        name = str(raw.get("name") or "")
        config = raw.get("config") if isinstance(raw.get("config"), Mapping) else {}
        if tool_type == "builtin" and name in BUILTIN_TOOL_TYPES:
            result.append({"type": BUILTIN_TOOL_TYPES[name], "name": name})
        elif tool_type == "mcp" and config.get("url"):
            result.append(
                {
                    "type": "remote_mcp",
                    "name": name,
                    "config": {"remoteMcp": {"url": str(config["url"])}},
                }
            )
        elif tool_type == "gateway":
            requested_id = str(config.get("gateway_id") or configured_gateway_id)
            if requested_id != configured_gateway_id or not resources.get("gateway_url"):
                raise ValueError(
                    "authenticated user policy identity is configured only for launchpad-gw"
                )
            if not attached_user_gateway:
                result.append(
                    {
                        "type": "remote_mcp",
                        "name": "launchpad_gw_user",
                        "config": {
                            "remoteMcp": {
                                "url": str(resources["gateway_url"]),
                                "headers": {"Authorization": f"Bearer {access_token}"},
                            }
                        },
                    }
                )
                attached_user_gateway = True

    if spec.get("knowledge_bases") and resources.get("kb_gateway_arn"):
        result.append(
            {
                "type": "agentcore_gateway",
                "name": "launchpad_kb_gw",
                "config": {
                    "agentCoreGateway": {
                        "gatewayArn": str(resources["kb_gateway_arn"]),
                        "outboundAuth": {
                            "oauth": {
                                "providerArn": str(resources["oauth_provider_arn"]),
                                "grantType": "CLIENT_CREDENTIALS",
                                "scopes": ["launchpad-gw/invoke"],
                            }
                        },
                    }
                },
            }
        )
    return result


def invoke_harness_text(
    client: Any,
    harness_arn: str,
    prompt: str,
    session_id: str | None = None,
    actor_id: str = "default",
) -> dict[str, Any]:
    """Synchronous invoke: send one user message, drain the event stream,
    return the concatenated assistant text plus session id."""
    session_id = session_id or new_session_id()
    response = client.invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=session_id,
        actorId=actor_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    text_parts: list[str] = []
    for event in response["stream"]:
        delta = event.get("contentBlockDelta", {}).get("delta", {})
        if "text" in delta:
            text_parts.append(delta["text"])
        if "runtimeClientError" in event:
            raise RuntimeError(f"runtime client error: {event['runtimeClientError']}")
        if "internalServerException" in event:
            raise RuntimeError(f"internal server error: {event['internalServerException']}")
    return {"text": "".join(text_parts), "session_id": session_id}
