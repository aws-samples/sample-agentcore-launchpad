"""Harness target-canary building blocks (pure, injected-client style).

A managed Harness cannot sit behind an ``http.agentcoreRuntime`` Gateway target
(the target validates a ``runtime/`` ARN), so a Harness canary fronts each version
with an HTTP **passthrough** target that SigV4-signs a call to InvokeHarness on a
named Harness endpoint. Verified live 2026-09-30 (us-west-2):

- the passthrough endpoint is ``https://bedrock-agentcore.<region>.amazonaws.com/
  harnesses/invoke`` with ``harnessArn`` + ``qualifier`` as static query
  parameters; a client POSTs the InvokeHarness JSON body to ``<gatewayUrl>/<target>/``
  (``/<target>/invocations`` is a 404 UnknownOperation; see ``TARGET_PATH_SUFFIX``);
- the gateway role is authorised as ``InvokeAgentRuntime`` on the harness ARN;
- a named endpoint's spans carry ``service.name = harness_<harnessName>.<endpoint>``
  and land in ``/aws/bedrock-agentcore/runtimes/<backingRuntimeId>-<endpoint>``;
- the session header survives the hop and the Harness spans carry the gateway's
  ``routing_experiment_variant_name`` (C / T1), so the A/B test attributes sessions.

Topology mirrors the Runtime canary (Model 1): one Harness, two immutable versions,
``control`` endpoint → the chosen earlier version, ``treatment`` endpoint → the
latest version (DEFAULT already serves it).
"""

import time
import uuid
from collections.abc import Callable
from typing import Any

from app.services.agentcore import harness as hc

Log = Callable[[str], None]
_sleep = time.sleep  # injectable
# POST to ``<gatewayUrl>/<target>/`` — the trailing slash is load-bearing: the A/B
# test's gatewayFilter ``/<target>/*`` only intercepts that form. A bare
# ``/<target>`` still reaches the Harness (HTTP 200) but bypasses the A/B test, so
# every session lands on control unattributed and the verdict never gets a sample
# (measured live 2026-09-30).
TARGET_PATH_SUFFIX = "/"
STICKY_HEADER = "$context.header.X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
STICKY_TIMEOUT_S = 3600
TRACES_SOURCE_SUFFIX = "-traces-source"
TRACES_DEST_SUFFIX = "-traces-destination"
# UpdateHarness fields a rollback re-publishes from the control version's config.
# Role, environment, authorizer and environment variables are left out (omit =
# keep): they describe where the Harness runs, not the behaviour under test.
_BEHAVIOUR_FIELDS = (
    "model", "systemPrompt", "tools", "skills", "allowedTools", "truncation",
    "maxIterations", "maxTokens", "timeoutSeconds",
)


def _noop(_msg: str) -> None:
    pass


# ─── naming ─────────────────────────────────────────────────────────────────
def control_endpoint(canary_id: str) -> str:
    """Harness endpoint names match ``[a-zA-Z][a-zA-Z0-9_]{0,47}``."""
    return f"ctl{canary_id[:6]}"


def treatment_endpoint(canary_id: str) -> str:
    return f"trt{canary_id[:6]}"


def endpoint_log_group(backing_runtime_id: str, endpoint: str) -> str:
    return f"/aws/bedrock-agentcore/runtimes/{backing_runtime_id}-{endpoint}"


def endpoint_service_name(harness_name: str, endpoint: str) -> str:
    return f"harness_{harness_name}.{endpoint}"


def backing_runtime_id(harness: dict[str, Any]) -> str:
    env = (harness.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}
    runtime_id = env.get("agentRuntimeId")
    if not runtime_id:
        raise RuntimeError("harness has no backing AgentCore Runtime environment")
    return str(runtime_id)


def invoke_body(prompt: str, *, actor_id: str | None = None,
                tools: list[dict[str, Any]] | None = None,
                allowed_tools: list[str] | None = None) -> dict[str, Any]:
    """The InvokeHarness JSON body the passthrough target relays verbatim."""
    body: dict[str, Any] = {"messages": [{"role": "user", "content": [{"text": prompt}]}]}
    if actor_id:
        body["actorId"] = actor_id
    if tools is not None:
        body["tools"] = tools
    if allowed_tools is not None:
        body["allowedTools"] = allowed_tools
    return body


# ─── versions ───────────────────────────────────────────────────────────────
def version_numbers(control: Any, harness_id: str) -> list[str]:
    """Every version of the Harness, ascending numerically."""
    rows = hc.list_harness_versions(control, harness_id)
    versions = {str(row.get("harnessVersion")) for row in rows if row.get("harnessVersion")}
    return sorted(versions, key=lambda v: (int(v) if v.isdigit() else 0, v))


def rollback_params(version_config: dict[str, Any], harness_id: str) -> dict[str, Any]:
    """UpdateHarness kwargs that re-publish one version's behaviour as a NEW version.

    ``memory`` must sit in ``{"optionalValue": …}`` (and an absent config means
    disabled); ``tools``/``skills`` are sent even when empty so the rollback detaches
    what the rejected version added (omit = keep)."""
    params: dict[str, Any] = {"harnessId": harness_id, "clientToken": str(uuid.uuid4())}
    for field in _BEHAVIOUR_FIELDS:
        value = version_config.get(field)
        if value is not None:
            params[field] = value
    params["memory"] = {"optionalValue": version_config.get("memory") or {"disabled": {}}}
    params.setdefault("tools", [])
    params.setdefault("skills", [])
    return params


# ─── gateway pieces ─────────────────────────────────────────────────────────
def create_passthrough_target(
    control: Any,
    *,
    gateway_id: str,
    name: str,
    harness_arn: str,
    qualifier: str,
    region: str,
    log: Log = _noop,
) -> str:
    """Create (or adopt) a passthrough target fronting one Harness endpoint; READY."""
    log(f"creating gateway target {name} → harness endpoint {qualifier}…")
    try:
        target = control.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=name,
            targetConfiguration={
                "http": {
                    "passthrough": {
                        "endpoint": f"https://bedrock-agentcore.{region}.amazonaws.com"
                                    "/harnesses/invoke",
                        "protocolType": "CUSTOM",
                        "staticQueryParameters": {
                            "harnessArn": harness_arn, "qualifier": qualifier,
                        },
                        "stickinessConfiguration": {
                            "identifier": STICKY_HEADER, "timeout": STICKY_TIMEOUT_S,
                        },
                    }
                }
            },
            credentialProviderConfigurations=[{
                "credentialProviderType": "GATEWAY_IAM_ROLE",
                "credentialProvider": {
                    "iamCredentialProvider": {"service": "bedrock-agentcore", "region": region}
                },
            }],
            clientToken=str(uuid.uuid4()),
        )
        target_id = target["targetId"]
    except Exception as exc:
        if type(exc).__name__ != "ConflictException":
            raise
        items = control.list_gateway_targets(gatewayIdentifier=gateway_id).get("items", [])
        target_id = next(t["targetId"] for t in items if t.get("name") == name)
    for _ in range(60):
        detail = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        status = detail.get("status")
        if status == "READY":
            return target_id
        if status in {"FAILED", "UPDATE_UNSUCCESSFUL"}:
            raise RuntimeError(f"gateway target {name} {status}: {detail.get('statusReasons')}")
        _sleep(5)
    raise TimeoutError(f"gateway target {name} not READY")


def trace_delivery_names(gateway_id: str) -> tuple[str, str]:
    return f"{gateway_id}{TRACES_SOURCE_SUFFIX}", f"{gateway_id}{TRACES_DEST_SUFFIX}"


def enable_gateway_tracing(logs: Any, *, gateway_id: str, gateway_arn: str,
                           log: Log = _noop) -> dict[str, str]:
    """Gateway spans → aws/spans (source TRACES → XRAY destination → delivery).

    The A/B test attributes a session to a variant from the gateway's routing
    attributes; AWS documents trace delivery as required for non-runtime targets."""
    source, dest = trace_delivery_names(gateway_id)
    log("enabling gateway trace delivery…")
    try:
        logs.put_delivery_source(name=source, logType="TRACES", resourceArn=gateway_arn)
    except Exception as exc:
        if type(exc).__name__ != "ConflictException":
            raise
    try:
        dest_arn = logs.put_delivery_destination(
            name=dest, deliveryDestinationType="XRAY"
        )["deliveryDestination"]["arn"]
    except Exception as exc:
        if type(exc).__name__ != "ConflictException":
            raise
        dest_arn = logs.get_delivery_destination(name=dest)["deliveryDestination"]["arn"]
    try:
        logs.create_delivery(deliverySourceName=source, deliveryDestinationArn=dest_arn)
    except Exception as exc:
        if type(exc).__name__ != "ConflictException":
            raise
    return {"source": source, "destination": dest}


def disable_gateway_tracing(logs: Any, gateway_id: str) -> list[dict[str, str]]:
    """Delete the delivery, source and destination; each failure is reported."""
    source, dest = trace_delivery_names(gateway_id)
    results: list[dict[str, str]] = []

    def attempt(category: str, fn: Callable[[], Any]) -> None:
        try:
            fn()
            results.append({"category": category, "status": "deleted", "detail": ""})
        except Exception as exc:
            if type(exc).__name__ == "ResourceNotFoundException":
                results.append({"category": category, "status": "absent", "detail": ""})
            else:
                results.append({"category": category, "status": "skipped",
                                "detail": f"{type(exc).__name__}: {exc}"})

    def delete_deliveries() -> None:
        # DescribeDeliveries pages account-wide (measured live: the canary's
        # delivery was not on the first page), so walk every page
        kwargs: dict[str, Any] = {}
        while True:
            page = logs.describe_deliveries(**kwargs)
            for delivery in page.get("deliveries", []):
                if delivery.get("deliverySourceName") == source:
                    logs.delete_delivery(id=delivery["id"])
            token = page.get("nextToken")
            if not token:
                return
            kwargs = {"nextToken": token}

    attempt(f"trace-delivery:{source}", delete_deliveries)
    attempt(f"trace-source:{source}", lambda: logs.delete_delivery_source(name=source))
    attempt(f"trace-destination:{dest}", lambda: logs.delete_delivery_destination(name=dest))
    return results


def ensure_log_group(logs: Any, name: str) -> None:
    """CreateOnlineEvaluationConfig refuses a log group that does not exist yet, and
    a fresh Harness endpoint only creates its group on the first invocation."""
    try:
        logs.create_log_group(logGroupName=name)
    except Exception as exc:
        if type(exc).__name__ != "ResourceAlreadyExistsException":
            raise
