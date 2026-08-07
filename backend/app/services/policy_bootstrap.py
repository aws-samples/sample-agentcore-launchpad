"""Governance bootstrap: Transaction Search + policy engine + Cedar policies.

Same idempotent ensure_* contract as the earlier bootstrap layers.
Cedar sources live in samples/policies/ (committed for customers);
__GATEWAY_ARN__ is substituted at bootstrap time.
"""

import time
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import REPO_ROOT

POLICY_ENGINE_NAME = "launchpad_pe"
POLICIES_DIR = REPO_ROOT / "samples" / "policies"
TRANSACTION_SEARCH_POLL_ATTEMPTS = 30
TRANSACTION_SEARCH_POLL_INTERVAL_S = 5

# Vended-log delivery naming, matching the convention already present in the
# account for memory/runtime resources (`<resource-id>-traces-source`).
TRACES_SOURCE_SUFFIX = "-traces-source"
TRACES_DEST_SUFFIX = "-traces-destination"

POLICIES = [
    {
        "name": "launchpad_baseline_allow",
        "file": "allow_gateway_tools.cedar",
        # Intentional broad baseline permit — triggers an ALLOW_ALL finding by
        # design (Cedar is default-deny; restrictions layer on with forbids).
        "validation_mode": "IGNORE_ALL_FINDINGS",
    },
    {
        "name": "launchpad_payout_admin_only",
        "file": "payout_admin_only.cedar",
        "validation_mode": "IGNORE_ALL_FINDINGS",
    },
]


def _wait_transaction_search_active(xray: Any) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for _ in range(TRANSACTION_SEARCH_POLL_ATTEMPTS):
        state = xray.get_trace_segment_destination()
        if state.get("Status") == "ACTIVE":
            break
        # Deliberate status polling interval; the loop has a fixed attempt cap.
        time.sleep(TRANSACTION_SEARCH_POLL_INTERVAL_S)  # nosemgrep: arbitrary-sleep
    return state


def ensure_transaction_search(xray: Any) -> dict[str, Any]:
    """CloudWatch Transaction Search = X-Ray segments destined to CW Logs."""
    state = xray.get_trace_segment_destination()
    if state.get("Destination") == "CloudWatchLogs" and state.get("Status") == "ACTIVE":
        return {"enabled": True, "changed": False, "status": state["Status"]}
    xray.update_trace_segment_destination(Destination="CloudWatchLogs")
    state = _wait_transaction_search_active(xray)
    return {"enabled": state.get("Status") == "ACTIVE", "changed": True,
            "status": state.get("Status")}


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "ClientError")
    return type(exc).__name__


def _absent(fn: Any, **kwargs: Any) -> bool:
    """True when the resource does not exist. Only ResourceNotFoundException is
    swallowed — anything else is a real problem and must surface."""
    try:
        fn(**kwargs)
    except ClientError as exc:
        if _error_code(exc) == "ResourceNotFoundException":
            return True
        raise
    return False


def _delivery_exists(
    logs: Any,
    source_name: str,
    destination_arn: str | None = None,
) -> bool:
    """DescribeDeliveries has no per-name lookup, so paginate and match."""
    token: str | None = None
    while True:
        kwargs = {"nextToken": token} if token else {}
        page = logs.describe_deliveries(**kwargs)
        for delivery in page.get("deliveries") or []:
            if delivery.get("deliverySourceName") != source_name:
                continue
            if (
                destination_arn is not None
                and delivery.get("deliveryDestinationArn") != destination_arn
            ):
                continue
            return True
        token = page.get("nextToken")
        if not token:
            return False


def gateway_trace_delivery_status(
    logs: Any,
    gateway_arn: str,
    gateway_id: str,
) -> dict[str, Any]:
    """Read the configured Gateway trace-delivery state without mutating AWS."""
    source_name = f"{gateway_id}{TRACES_SOURCE_SUFFIX}"
    dest_name = f"{gateway_id}{TRACES_DEST_SUFFIX}"
    try:
        try:
            source = logs.get_delivery_source(name=source_name)["deliverySource"]
        except ClientError as exc:
            if _error_code(exc) == "ResourceNotFoundException":
                return {"status": "missing", "reason": "delivery_source_missing"}
            raise
        if (
            source.get("logType") != "TRACES"
            or gateway_arn not in (source.get("resourceArns") or [])
        ):
            return {"status": "missing", "reason": "delivery_source_invalid"}

        try:
            destination = logs.get_delivery_destination(name=dest_name)[
                "deliveryDestination"
            ]
        except ClientError as exc:
            if _error_code(exc) == "ResourceNotFoundException":
                return {"status": "missing", "reason": "delivery_destination_missing"}
            raise
        if (
            destination.get("deliveryDestinationType") != "XRAY"
            or not destination.get("arn")
        ):
            return {"status": "missing", "reason": "delivery_destination_invalid"}

        if not _delivery_exists(logs, source_name, destination.get("arn")):
            return {"status": "missing", "reason": "delivery_missing"}
    except (ClientError, BotoCoreError) as exc:
        return {"status": "unknown", "reason": _error_code(exc)}

    return {"status": "ready", "reason": None}


def ensure_gateway_traces(
    logs: Any,
    gateway_arn: str,
    gateway_id: str,
    *,
    transaction_search_enabled: bool,
) -> dict[str, Any]:
    """Idempotently open the Policy/Gateway span channel for one Gateway.

    AgentCore emits Policy decision spans only once *trace delivery* is enabled on
    the attached Gateway. That is not a Gateway field — no UpdateGateway is
    involved — but a CloudWatch vended-log delivery: source (logType=TRACES) →
    destination (XRAY) → delivery. Spans then land in the shared `aws/spans` group.

    Returns a ``status`` rather than a bool because the caller must be able to tell
    "already open" from "deliberately skipped" from "attempted and failed"; the
    follow-up span work depends on knowing which. A failure here is reported, not
    raised: the platform is usable without spans, so telemetry must not abort
    bootstrap — but it must never be reported as success either.
    """
    source_name = f"{gateway_id}{TRACES_SOURCE_SUFFIX}"
    dest_name = f"{gateway_id}{TRACES_DEST_SUFFIX}"
    if not transaction_search_enabled:
        # AWS documents Transaction Search as a hard prerequisite for tracing.
        return {
            "status": "skipped",
            "changed": False,
            "reason": "transaction_search_disabled",
            "source": source_name,
            "destination": dest_name,
        }

    changed = False
    try:
        if _absent(logs.get_delivery_source, name=source_name):
            logs.put_delivery_source(
                name=source_name, logType="TRACES", resourceArn=gateway_arn
            )
            changed = True

        # XRAY destinations carry no destinationResourceArn — spans are routed to
        # the shared aws/spans group, not to a per-gateway log group.
        if _absent(logs.get_delivery_destination, name=dest_name):
            created = logs.put_delivery_destination(
                name=dest_name, deliveryDestinationType="XRAY"
            )
            dest_arn = created["deliveryDestination"]["arn"]
            changed = True
        else:
            dest_arn = logs.get_delivery_destination(name=dest_name)[
                "deliveryDestination"
            ]["arn"]

        delivery_id = None
        if not _delivery_exists(logs, source_name, dest_arn):
            delivery = logs.create_delivery(
                deliverySourceName=source_name, deliveryDestinationArn=dest_arn
            )
            delivery_id = delivery.get("delivery", {}).get("id")
            changed = True
    except ClientError as exc:
        code = _error_code(exc)
        if code == "ConflictException":
            # A concurrent or prior run won the race — the desired end state.
            return {
                "status": "present",
                "changed": False,
                "source": source_name,
                "destination": dest_name,
            }
        return {
            "status": "failed",
            "changed": changed,
            "reason": code,
            "source": source_name,
            "destination": dest_name,
        }
    except BotoCoreError as exc:
        return {
            "status": "failed",
            "changed": changed,
            "reason": _error_code(exc),
            "source": source_name,
            "destination": dest_name,
        }

    return {
        "status": "created" if changed else "present",
        "changed": changed,
        "source": source_name,
        "destination": dest_name,
        "delivery_id": delivery_id,
    }


def ensure_policy_engine(control: Any, name: str = POLICY_ENGINE_NAME) -> tuple[dict, bool]:
    engines = control.list_policy_engines(maxResults=20).get("policyEngines", [])
    for engine in engines:
        if engine.get("name") == name:
            return (
                {"id": engine["policyEngineId"], "arn": engine["policyEngineArn"]},
                False,
            )
    created = control.create_policy_engine(
        name=name, description="AgentCore Launchpad governance — Cedar tool authorization"
    )
    engine_id = created["policyEngineId"]
    _wait_engine_active(control, engine_id)
    return {"id": engine_id, "arn": created["policyEngineArn"]}, True


def _wait_engine_active(control: Any, engine_id: str, timeout_s: int = 120) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = control.get_policy_engine(policyEngineId=engine_id)["status"]
        if status == "ACTIVE":
            return
        if "FAILED" in status:
            raise RuntimeError(f"policy engine {engine_id} entered {status}")
        # Deliberate polling interval; the surrounding loop owns the deadline.
        time.sleep(3)  # nosemgrep: arbitrary-sleep
    raise TimeoutError(f"policy engine {engine_id} not ACTIVE after {timeout_s}s")


def render_policy_statement(filename: str, gateway_arn: str) -> str:
    source = (POLICIES_DIR / filename).read_text(encoding="utf-8")
    return source.replace("__GATEWAY_ARN__", gateway_arn)


def ensure_policies(
    control: Any, engine_id: str, gateway_arn: str
) -> list[dict[str, Any]]:
    existing = {
        p["name"]: p
        for p in control.list_policies(policyEngineId=engine_id, maxResults=20).get(
            "policies", []
        )
    }
    results = []
    for spec in POLICIES:
        if spec["name"] in existing:
            results.append({"name": spec["name"], "id": existing[spec["name"]]["policyId"],
                            "created": False})
            continue
        statement = render_policy_statement(spec["file"], gateway_arn)
        created = control.create_policy(
            policyEngineId=engine_id,
            name=spec["name"],
            definition={"cedar": {"statement": statement}},
            validationMode=spec["validation_mode"],
        )
        _wait_policy_settled(control, engine_id, created["policyId"])
        results.append({"name": spec["name"], "id": created["policyId"], "created": True})
    return results


def _wait_policy_settled(
    control: Any, engine_id: str, policy_id: str, timeout_s: int = 120
) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        detail = control.get_policy(policyEngineId=engine_id, policyId=policy_id)
        status = detail["status"]
        if status == "ACTIVE":
            return
        if "FAILED" in status:
            raise RuntimeError(
                f"policy {policy_id} entered {status}: {detail.get('statusReasons')}"
            )
        # Deliberate polling interval; the surrounding loop owns the deadline.
        time.sleep(3)  # nosemgrep: arbitrary-sleep
    raise TimeoutError(f"policy {policy_id} not ACTIVE after {timeout_s}s")


def attach_engine_to_gateway(
    control: Any, gateway_id: str, engine_arn: str, mode: str = "ENFORCE"
) -> bool:
    """Attach (or confirm) the policy engine on the gateway. Returns changed."""
    gateway = control.get_gateway(gatewayIdentifier=gateway_id)
    current = gateway.get("policyEngineConfiguration") or {}
    if current.get("arn") == engine_arn and current.get("mode") == mode:
        return False
    control.update_gateway(
        gatewayIdentifier=gateway_id,
        name=gateway["name"],
        roleArn=gateway["roleArn"],
        protocolType=gateway.get("protocolType", "MCP"),
        authorizerType=gateway["authorizerType"],
        authorizerConfiguration=gateway["authorizerConfiguration"],
        policyEngineConfiguration={"arn": engine_arn, "mode": mode},
    )
    deadline = time.time() + 180
    while time.time() < deadline:
        if control.get_gateway(gatewayIdentifier=gateway_id)["status"] == "READY":
            return True
        # Deliberate polling interval; the surrounding loop owns the deadline.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    raise TimeoutError("gateway not READY after policy engine attach")


def run_policy_bootstrap(
    control: Any, xray: Any, config: dict[str, Any], *, logs: Any
) -> dict[str, Any]:
    resources = config.get("resources", {})
    tx = ensure_transaction_search(xray)
    engine, engine_created = ensure_policy_engine(control)
    policies = ensure_policies(control, engine["id"], resources["gateway_arn"])
    attached = attach_engine_to_gateway(control, resources["gateway_id"], engine["arn"])
    if tx.get("changed") and not tx.get("enabled"):
        # Fresh accounts can outlast the first 150-second poll. Policy setup gives
        # the destination more time to settle; reconcile once before deciding
        # whether Gateway trace delivery must be skipped.
        state = _wait_transaction_search_active(xray)
        tx = {
            "enabled": state.get("Status") == "ACTIVE",
            "changed": True,
            "status": state.get("Status"),
        }
    # Opens the Policy span channel. Runs after Transaction Search because AWS
    # requires it, and after the engine attach because spans are only meaningful
    # once a Policy engine evaluates the Gateway's calls.
    traces = ensure_gateway_traces(
        logs,
        resources["gateway_arn"],
        resources["gateway_id"],
        transaction_search_enabled=bool(tx.get("enabled")),
    )
    return {
        "transaction_search": tx,
        "policy_engine": {**engine, "created": engine_created},
        "policies": policies,
        "gateway_attached": attached,
        "gateway_traces": traces,
    }
