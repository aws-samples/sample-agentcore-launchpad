"""Product rules for existing Gateway and Policy governance."""

import json
import re
import time
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.models.ledger import PolicyChange
from app.routers.auth import enabled as auth_enabled
from app.services import registry_console
from app.services.agentcore import policy as policy_api
from app.services.agentcore.client import control_client, iam_client

POLICY_IAM_ACTIONS = (
    "bedrock-agentcore:GetPolicyEngine",
    "bedrock-agentcore:AuthorizeAction",
    "bedrock-agentcore:PartiallyAuthorizeActions",
)

_GATEWAY_CACHE_TTL_S = 30.0
_gateway_cache: dict[str, Any] = {"key": None, "at": 0.0, "data": None}


def operator_identity(settings: Settings | None = None) -> str:
    current = settings or get_settings()
    return current.auth_username if auth_enabled(current) else "local-operator"


def iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def json_snapshot(value: Any) -> Any:
    """Return a JSON-safe snapshot for the immutable audit journal."""
    return json.loads(json.dumps(value, default=iso))


def engine_id_from_arn(arn: str | None) -> str | None:
    if not arn:
        return None
    return arn.rstrip("/").rsplit("/", 1)[-1]


def invalidate_gateway_cache() -> None:
    _gateway_cache.update(key=None, at=0.0, data=None)


def _attachability(gateway: dict[str, Any], settings: Settings) -> dict[str, Any]:
    authorizer = gateway.get("authorizerType")
    if authorizer == "AWS_IAM":
        return {"attachable": True, "reason": None, "auth_type": "aws_iam"}
    if authorizer == "NONE":
        return {"attachable": True, "reason": None, "auth_type": "none"}
    resources = settings.resources
    if (
        gateway.get("gatewayId") == resources.get("gateway_id")
        and resources.get("oauth_provider_arn")
    ):
        return {"attachable": True, "reason": None, "auth_type": "oauth"}
    if authorizer == "CUSTOM_JWT":
        return {
            "attachable": False,
            "reason": "custom_jwt_provider_unmanaged",
            "auth_type": "oauth",
        }
    return {
        "attachable": False,
        "reason": "authorizer_unsupported",
        "auth_type": None,
    }


def _all_mcp_gateway_details(control: Any) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for summary in policy_api.list_gateways(control):
        if summary.get("protocolType") not in (None, "MCP"):
            continue
        detail = policy_api.get_gateway(control, summary["gatewayId"])
        if detail.get("protocolType") == "MCP":
            details.append(detail)
    return details


def _engine_impact(
    gateway_details: list[dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    impact: dict[str, list[dict[str, str]]] = {}
    for gateway in gateway_details:
        engine_arn = (gateway.get("policyEngineConfiguration") or {}).get("arn")
        if not engine_arn:
            continue
        impact.setdefault(engine_arn, []).append(
            {
                "id": gateway["gatewayId"],
                "name": gateway["name"],
                "arn": gateway["gatewayArn"],
            }
        )
    for gateways in impact.values():
        gateways.sort(key=lambda item: (item["name"], item["id"]))
    return impact


def _engine_view(
    control: Any,
    attachment: dict[str, Any],
    cache: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    arn = attachment.get("arn")
    engine_id = engine_id_from_arn(arn)
    if not arn or not engine_id:
        return None
    if engine_id not in cache:
        cache[engine_id] = policy_api.get_policy_engine(control, engine_id)
    detail = cache[engine_id]
    return {
        "id": engine_id,
        "arn": arn,
        "name": detail.get("name"),
        "status": detail.get("status"),
        "status_reasons": detail.get("statusReasons") or [],
        "updated_at": iso(detail.get("updatedAt")),
        "mode": attachment.get("mode"),
    }


def _gateway_summary(
    control: Any,
    gateway: dict[str, Any],
    *,
    settings: Settings,
    impact: dict[str, list[dict[str, str]]],
    engine_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    tags = policy_api.list_tags(control, gateway["gatewayArn"])
    targets = policy_api.list_gateway_targets(control, gateway["gatewayId"])
    attachment = gateway.get("policyEngineConfiguration") or {}
    engine_arn = attachment.get("arn")
    affected = impact.get(engine_arn, []) if engine_arn else []
    configured_gateway_id = settings.resources.get("gateway_id")
    configured_gateway_url = settings.resources.get("gateway_url")
    return {
        "id": gateway["gatewayId"],
        "arn": gateway["gatewayArn"],
        "name": gateway["name"],
        "url": gateway.get("gatewayUrl"),
        "description": gateway.get("description") or "",
        "status": gateway.get("status"),
        "status_reasons": gateway.get("statusReasons") or [],
        "updated_at": iso(gateway.get("updatedAt")),
        "protocol_type": gateway.get("protocolType"),
        "authorizer_type": gateway.get("authorizerType"),
        "role_arn": gateway.get("roleArn"),
        "managed": policy_api.is_managed(tags),
        "target_count": len(targets),
        "targets": [
            {
                "id": target.get("targetId"),
                "name": target.get("name"),
                "status": target.get("status"),
                "description": target.get("description") or "",
            }
            for target in targets
        ],
        "policy_engine": _engine_view(control, attachment, engine_cache),
        "shared_gateways": affected,
        "shared_engine": len(affected) > 1,
        "attachability": _attachability(gateway, settings),
        "policy_test_available": bool(
            configured_gateway_id
            and configured_gateway_url
            and gateway["gatewayId"] == configured_gateway_id
            and gateway.get("gatewayUrl") == configured_gateway_url
        ),
    }


def list_gateway_views(
    control: Any,
    *,
    settings: Settings | None = None,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    current = settings or get_settings()
    cache_key = (id(control), current.region, current.account_id)
    if (
        not refresh
        and _gateway_cache["data"] is not None
        and _gateway_cache["key"] == cache_key
        and time.monotonic() - _gateway_cache["at"] < _GATEWAY_CACHE_TTL_S
    ):
        return _gateway_cache["data"]

    details = _all_mcp_gateway_details(control)
    impact = _engine_impact(details)
    engine_cache: dict[str, dict[str, Any]] = {}
    registry_id = current.resources.get("registry_id")
    registry_states = (
        registry_console.gateway_registry_states(
            gateways=details,
            registry_id=registry_id,
        )
        if registry_id
        else {}
    )
    views = [
        {
            **_gateway_summary(
                control,
                detail,
                settings=current,
                impact=impact,
                engine_cache=engine_cache,
            ),
            **registry_states.get(
                detail["gatewayId"],
                {"registry_record": None, "legacy_record_count": 0},
            ),
        }
        for detail in details
    ]
    views.sort(key=lambda item: (not item["managed"], item["name"], item["id"]))
    _gateway_cache.update(key=cache_key, at=time.monotonic(), data=views)
    return views


def _require_gateway(control: Any, gateway_id: str) -> dict[str, Any]:
    try:
        gateway = policy_api.get_gateway(control, gateway_id)
    except control.exceptions.ResourceNotFoundException as exc:
        raise NotFoundError(
            "governance.gateway_not_found",
            f"Gateway {gateway_id} was not found",
        ) from exc
    if gateway.get("protocolType") != "MCP":
        raise AppError(
            "governance.gateway_unsupported",
            "Only MCP Gateways can be managed",
            status_code=409,
        )
    return gateway


def _require_managed(control: Any, gateway_id: str) -> dict[str, Any]:
    gateway = _require_gateway(control, gateway_id)
    tags = policy_api.list_tags(control, gateway["gatewayArn"])
    if not policy_api.is_managed(tags):
        raise AppError(
            "governance.gateway_not_managed",
            "Manage the Gateway before changing Registry or Policy resources",
            status_code=409,
        )
    return gateway


def manage_gateway(control: Any, gateway_id: str) -> dict[str, Any]:
    gateway = _require_gateway(control, gateway_id)
    policy_api.tag_managed(control, gateway["gatewayArn"])
    invalidate_gateway_cache()
    return {"gateway_id": gateway_id, "managed": True}


def unmanage_gateway(control: Any, gateway_id: str) -> dict[str, Any]:
    gateway = _require_gateway(control, gateway_id)
    policy_api.untag_managed(control, gateway["gatewayArn"])
    invalidate_gateway_cache()
    return {"gateway_id": gateway_id, "managed": False}


def discover_actions(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(
        target: dict[str, Any],
        tool_name: str,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> None:
        target_name = str(target.get("name") or "")
        if not target_name or not tool_name:
            return
        name = tool_name if "___" in tool_name else f"{target_name}___{tool_name}"
        if name in seen:
            return
        seen.add(name)
        actions.append(
            {
                "name": name,
                "target_id": str(target.get("targetId") or ""),
                "target_name": target_name,
                "description": description,
                "input_schema": input_schema or {},
                "verified": True,
                "source": "control_schema",
            }
        )

    for target in targets:
        mcp = (target.get("targetConfiguration") or {}).get("mcp") or {}
        lambda_schema = ((mcp.get("lambda") or {}).get("toolSchema") or {}).get(
            "inlinePayload"
        )
        if isinstance(lambda_schema, list):
            for tool in lambda_schema:
                if not isinstance(tool, dict):
                    continue
                add(
                    target,
                    str(tool.get("name") or ""),
                    str(tool.get("description") or ""),
                    tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {},
                )

        openapi = (mcp.get("openApiSchema") or {}).get("inlinePayload")
        if isinstance(openapi, str):
            try:
                document = json.loads(openapi)
            except json.JSONDecodeError:
                document = {}
            for path_item in (document.get("paths") or {}).values():
                if not isinstance(path_item, dict):
                    continue
                for operation in path_item.values():
                    if isinstance(operation, dict) and operation.get("operationId"):
                        add(
                            target,
                            str(operation["operationId"]),
                            str(operation.get("description") or operation.get("summary") or ""),
                        )

        static_mcp = ((mcp.get("mcpServer") or {}).get("mcpToolSchema") or {}).get(
            "inlinePayload"
        )
        if isinstance(static_mcp, str):
            try:
                document = json.loads(static_mcp)
            except json.JSONDecodeError:
                document = {}
            for tool in document.get("tools") or []:
                if isinstance(tool, dict):
                    add(
                        target,
                        str(tool.get("name") or ""),
                        str(tool.get("description") or ""),
                        tool.get("inputSchema")
                        if isinstance(tool.get("inputSchema"), dict)
                        else {},
                    )

        smithy = (mcp.get("smithyModel") or {}).get("inlinePayload")
        if isinstance(smithy, str):
            try:
                document = json.loads(smithy)
            except json.JSONDecodeError:
                document = {}
            for shape_name, shape in (document.get("shapes") or {}).items():
                if isinstance(shape, dict) and shape.get("type") == "operation":
                    add(target, str(shape_name).rsplit("#", 1)[-1])

        for configuration in (mcp.get("connector") or {}).get("configurations") or []:
            if isinstance(configuration, dict) and configuration.get("name"):
                add(
                    target,
                    str(configuration["name"]),
                    str(configuration.get("description") or ""),
                )

    return sorted(actions, key=lambda item: item["name"])


def iam_preflight(
    iam: Any,
    *,
    role_arn: str | None,
    engine_arn: str,
    gateway_arn: str,
) -> dict[str, Any]:
    statements = [
        {
            "Effect": "Allow",
            "Action": ["bedrock-agentcore:GetPolicyEngine"],
            "Resource": engine_arn,
        },
        {
            "Effect": "Allow",
            "Action": [
                "bedrock-agentcore:AuthorizeAction",
                "bedrock-agentcore:PartiallyAuthorizeActions",
            ],
            "Resource": [engine_arn, gateway_arn],
        },
    ]
    if not role_arn:
        return {
            "status": "unknown",
            "missing_actions": list(POLICY_IAM_ACTIONS),
            "reason": "gateway_role_missing",
            "remediation": {"Version": "2012-10-17", "Statement": statements},
        }
    try:
        engine_response = iam.simulate_principal_policy(
            PolicySourceArn=role_arn,
            ActionNames=["bedrock-agentcore:GetPolicyEngine"],
            ResourceArns=[engine_arn],
        )
        authorization_response = iam.simulate_principal_policy(
            PolicySourceArn=role_arn,
            ActionNames=[
                "bedrock-agentcore:AuthorizeAction",
                "bedrock-agentcore:PartiallyAuthorizeActions",
            ],
            ResourceArns=[engine_arn, gateway_arn],
        )
    except (ClientError, BotoCoreError) as exc:
        code = (
            exc.response.get("Error", {}).get("Code")
            if isinstance(exc, ClientError)
            else type(exc).__name__
        )
        return {
            "status": "unknown",
            "missing_actions": list(POLICY_IAM_ACTIONS),
            "reason": "simulation_denied",
            "operator_error": code,
            "remediation": {"Version": "2012-10-17", "Statement": statements},
        }
    decisions: dict[str, list[str]] = {}
    for response in (engine_response, authorization_response):
        for result in response.get("EvaluationResults") or []:
            action = result.get("EvalActionName")
            if action in POLICY_IAM_ACTIONS:
                decisions.setdefault(action, []).append(result.get("EvalDecision"))
    missing = [
        action
        for action in POLICY_IAM_ACTIONS
        if not decisions.get(action)
        or any(decision != "allowed" for decision in decisions[action])
    ]
    return {
        "status": "fail" if missing else "pass",
        "missing_actions": missing,
        "reason": "role_permissions_missing" if missing else None,
        "remediation": {"Version": "2012-10-17", "Statement": statements},
    }


def external_tools_list_command(
    gateway: dict[str, Any],
    settings: Settings,
) -> str | None:
    """Operator-side ``tools/list`` template for external CUSTOM_JWT Gateways.

    Launchpad never accepts, stores, or proxies an external bearer token, so
    dynamic targets whose exact actions are unavailable from the control plane
    are discovered by the operator outside the console. The template only
    references an operator-side environment variable.
    """
    url = gateway.get("gatewayUrl")
    if gateway.get("authorizerType") != "CUSTOM_JWT" or not url:
        return None
    if gateway.get("gatewayId") == settings.resources.get("gateway_id"):
        return None
    return (
        f"curl -sS -X POST '{url}' \\\n"
        '  -H "Authorization: Bearer $GATEWAY_ACCESS_TOKEN" \\\n'
        "  -H 'Content-Type: application/json' \\\n"
        "  -H 'Accept: application/json, text/event-stream' \\\n"
        '  -d \'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\''
    )


def gateway_detail(
    control: Any,
    iam: Any,
    gateway_id: str,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    current = settings or get_settings()
    gateway = _require_gateway(control, gateway_id)
    all_gateways = _all_mcp_gateway_details(control)
    impact = _engine_impact(all_gateways)
    engine_cache: dict[str, dict[str, Any]] = {}
    summary = _gateway_summary(
        control,
        gateway,
        settings=current,
        impact=impact,
        engine_cache=engine_cache,
    )
    registry_id = current.resources.get("registry_id")
    registry_state = (
        registry_console.gateway_registry_states(
            gateways=[gateway],
            registry_id=registry_id,
        ).get(gateway_id)
        if registry_id
        else None
    )
    targets = policy_api.list_gateway_target_details(control, gateway_id)
    attachment = gateway.get("policyEngineConfiguration") or {}
    engine_arn = attachment.get("arn")
    preflight = None
    if engine_arn:
        preflight = iam_preflight(
            iam,
            role_arn=gateway.get("roleArn"),
            engine_arn=engine_arn,
            gateway_arn=gateway["gatewayArn"],
        )
    return {
        **summary,
        **(registry_state or {"registry_record": None, "legacy_record_count": 0}),
        "authorizer_configuration": gateway.get("authorizerConfiguration"),
        "protocol_configuration": gateway.get("protocolConfiguration"),
        "targets": [
            {
                "id": target.get("targetId"),
                "name": target.get("name"),
                "status": target.get("status"),
                "status_reasons": target.get("statusReasons") or [],
                "description": target.get("description") or "",
                "listing_mode": (
                    ((target.get("targetConfiguration") or {}).get("mcp") or {})
                    .get("mcpServer", {})
                    .get("listingMode")
                ),
            }
            for target in targets
        ],
        "actions": discover_actions(targets),
        "iam_preflight": preflight,
        "external_tools_list_command": external_tools_list_command(gateway, current),
    }


def assert_managed_gateway(control: Any, gateway_id: str) -> dict[str, Any]:
    return _require_managed(control, gateway_id)


def _gateway_registry_input(
    control: Any,
    gateway: dict[str, Any],
) -> dict[str, Any]:
    gateway_url = gateway.get("gatewayUrl")
    if not gateway_url:
        raise AppError(
            "governance.gateway_unsupported",
            "Gateway has no streamable MCP endpoint",
            status_code=409,
        )
    targets = policy_api.list_gateway_target_details(control, gateway["gatewayId"])
    return {
        "gateway_id": gateway["gatewayId"],
        "gateway_name": gateway["name"],
        "gateway_url": gateway_url,
        "target_names": [
            target["name"] for target in targets if target.get("name")
        ],
        "actions": discover_actions(targets),
    }


def gateway_registry_preview(
    control: Any,
    gateway_id: str,
    *,
    record_name: str | None = None,
) -> dict[str, Any]:
    gateway = _require_gateway(control, gateway_id)
    return registry_console.gateway_registry_preview(
        **_gateway_registry_input(control, gateway),
        record_name=record_name,
    )


def import_gateway_registry(
    control: Any,
    gateway_id: str,
    request: Any,
) -> dict[str, Any]:
    gateway = _require_managed(control, gateway_id)
    _assert_gateway_ready(gateway)
    _assert_updated_at(
        request.expected_gateway_updated_at,
        gateway.get("updatedAt"),
        "Gateway",
    )
    result = registry_console.import_gateway_record(
        **_gateway_registry_input(control, gateway),
        record_name=request.record_name,
        apply_update=request.apply_update,
    )
    invalidate_gateway_cache()
    return result


def retire_gateway_legacy_records(
    control: Any,
    gateway_id: str,
    request: Any,
) -> dict[str, Any]:
    gateway = _require_managed(control, gateway_id)
    _assert_gateway_ready(gateway)
    _assert_updated_at(
        request.expected_gateway_updated_at,
        gateway.get("updatedAt"),
        "Gateway",
    )
    registry_id = get_settings().resources.get("registry_id")
    states = registry_console.gateway_registry_states(
        gateways=[gateway],
        registry_id=registry_id,
    )
    gateway_record = states.get(gateway_id, {}).get("registry_record")
    if not gateway_record:
        raise AppError(
            "governance.registry_record_not_approved",
            "Gateway-level Registry record was not found",
            status_code=409,
        )
    result = registry_console.retire_legacy_gateway_records(
        gateway_record_id=gateway_record["record_id"],
        legacy_record_ids=request.record_ids,
        registry_id=registry_id,
    )
    invalidate_gateway_cache()
    return result


def assert_shared_engine_acknowledged(
    control: Any,
    engine_arn: str,
    acknowledged_gateway_ids: list[str],
) -> list[dict[str, str]]:
    impact = _engine_impact(_all_mcp_gateway_details(control)).get(engine_arn, [])
    expected = {gateway["id"] for gateway in impact}
    if set(acknowledged_gateway_ids) != expected:
        raise AppError(
            "governance.shared_engine_changed",
            "The Gateways sharing this Policy Engine changed; review the impact again",
            {"shared_gateways": impact},
            status_code=409,
        )
    return impact


class PartialOperation(RuntimeError):
    def __init__(self, message: str, after: dict[str, Any]):
        super().__init__(message)
        self.after = after


def _normalized_at(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _assert_updated_at(expected: Any, live: Any, label: str) -> None:
    if _normalized_at(expected) != _normalized_at(live):
        raise AppError(
            "governance.concurrent_change",
            f"{label} changed after it was loaded; refresh before retrying",
            {"expected": iso(expected), "current": iso(live)},
            status_code=409,
        )


def _assert_gateway_ready(gateway: dict[str, Any]) -> None:
    if gateway.get("status") != "READY":
        raise AppError(
            "governance.gateway_not_ready",
            f"Gateway is {gateway.get('status')}; wait for READY",
            {"status_reasons": gateway.get("statusReasons") or []},
            status_code=409,
        )


def _statement(detail: dict[str, Any]) -> str:
    definition = detail.get("definition") or {}
    return (
        (definition.get("cedar") or {}).get("statement")
        or (definition.get("policy") or {}).get("statement")
        or ""
    )


def _policy_snapshot(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": detail.get("policyId"),
        "arn": detail.get("policyArn"),
        "name": detail.get("name"),
        "status": detail.get("status"),
        "status_reasons": detail.get("statusReasons") or [],
        "enforcement_mode": detail.get("enforcementMode"),
        "statement": _statement(detail),
        "description": detail.get("description") or "",
        "updated_at": iso(detail.get("updatedAt")),
    }


def _gateway_policy_snapshot(gateway: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": gateway.get("gatewayId"),
        "arn": gateway.get("gatewayArn"),
        "name": gateway.get("name"),
        "status": gateway.get("status"),
        "updated_at": iso(gateway.get("updatedAt")),
        "policy_engine_configuration": json_snapshot(
            gateway.get("policyEngineConfiguration")
        ),
    }


def _engine_snapshot(engine: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": engine.get("policyEngineId"),
        "arn": engine.get("policyEngineArn"),
        "name": engine.get("name"),
        "status": engine.get("status"),
        "status_reasons": engine.get("statusReasons") or [],
        "updated_at": iso(engine.get("updatedAt")),
    }


def _attached_engine(
    control: Any,
    gateway: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    attachment = gateway.get("policyEngineConfiguration") or {}
    engine_id = engine_id_from_arn(attachment.get("arn"))
    if not engine_id:
        raise AppError(
            "governance.policy_engine_missing",
            "Attach a Policy Engine before managing policies",
            status_code=409,
        )
    engine = policy_api.get_policy_engine(control, engine_id)
    if engine.get("status") != "ACTIVE":
        raise AppError(
            "governance.policy_not_settled",
            f"Policy Engine is {engine.get('status')}; wait for ACTIVE",
            {"status_reasons": engine.get("statusReasons") or []},
            status_code=409,
        )
    return attachment, engine


def _assert_shared_for_mutation(
    control: Any,
    engine_arn: str,
    acknowledged_gateway_ids: list[str],
) -> list[dict[str, str]]:
    impact = _engine_impact(_all_mcp_gateway_details(control)).get(engine_arn, [])
    if len(impact) <= 1:
        return impact
    expected = {gateway["id"] for gateway in impact}
    if set(acknowledged_gateway_ids) != expected:
        raise AppError(
            "governance.shared_engine_changed",
            "Confirm every Gateway affected by this shared Policy Engine",
            {"shared_gateways": impact},
            status_code=409,
        )
    return impact


def _change_out(change: PolicyChange) -> dict[str, Any]:
    return {
        "id": change.id,
        "gateway_id": change.gateway_id,
        "gateway_name": change.gateway_name,
        "engine_id": change.engine_id,
        "policy_id": change.policy_id,
        "candidate_policy_id": change.candidate_policy_id,
        "operation": change.operation,
        "operator": change.operator,
        "status": change.status,
        "before": change.before,
        "requested": change.requested,
        "after": change.after,
        "expected_updated_at": change.expected_updated_at,
        "override_reason": change.override_reason,
        "error": change.error,
        "created_at": iso(change.created_at),
        "started_at": iso(change.started_at),
        "completed_at": iso(change.completed_at),
    }


def _new_change(
    db: Session,
    *,
    gateway: dict[str, Any],
    operation: str,
    before: dict[str, Any],
    requested: dict[str, Any],
    engine: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    override_reason: str | None = None,
) -> PolicyChange:
    running = (
        db.query(PolicyChange)
        .filter(
            PolicyChange.gateway_id == gateway["gatewayId"],
            PolicyChange.status.in_(("pending", "running")),
        )
        .first()
    )
    if running is not None:
        raise AppError(
            "governance.operation_in_flight",
            f"Operation {running.id} is still {running.status}",
            {"operation": _change_out(running)},
            status_code=409,
        )
    change = PolicyChange(
        gateway_id=gateway["gatewayId"],
        gateway_arn=gateway["gatewayArn"],
        gateway_name=gateway["name"],
        engine_id=(engine or {}).get("policyEngineId"),
        engine_arn=(engine or {}).get("policyEngineArn"),
        policy_id=(policy or {}).get("policyId"),
        policy_name=(policy or {}).get("name"),
        operation=operation,
        operator=operator_identity(),
        status="pending",
        before=json_snapshot(before),
        requested=json_snapshot(requested),
        expected_updated_at=requested.get("expected_gateway_updated_at")
        or requested.get("expected_policy_updated_at"),
        override_reason=override_reason,
    )
    db.add(change)
    db.commit()
    db.refresh(change)
    return change


def get_operation(db: Session, operation_id: str) -> dict[str, Any]:
    change = db.get(PolicyChange, operation_id)
    if change is None:
        raise NotFoundError(
            "governance.operation_not_found",
            f"Operation {operation_id} was not found",
        )
    return _change_out(change)


def list_audit(db: Session, gateway_id: str) -> list[dict[str, Any]]:
    return [
        _change_out(change)
        for change in (
            db.query(PolicyChange)
            .filter(PolicyChange.gateway_id == gateway_id)
            .order_by(PolicyChange.created_at.desc())
            .limit(100)
            .all()
        )
    ]


def policies_view(
    control: Any,
    gateway_id: str,
    *,
    db: Session | None = None,
) -> dict[str, Any]:
    gateway = _require_gateway(control, gateway_id)
    attachment = gateway.get("policyEngineConfiguration") or {}
    engine_id = engine_id_from_arn(attachment.get("arn"))
    if not engine_id:
        return {
            "gateway": _gateway_policy_snapshot(gateway),
            "engine": None,
            "policies": [],
        }
    engine = policy_api.get_policy_engine(control, engine_id)
    relations: dict[str, dict[str, str]] = {}
    if db is not None:
        rows = (
            db.query(PolicyChange)
            .filter(
                PolicyChange.gateway_id == gateway_id,
                PolicyChange.candidate_policy_id.is_not(None),
            )
            .order_by(PolicyChange.created_at.desc())
            .all()
        )
        for row in rows:
            if row.candidate_policy_id and row.policy_id:
                relations.setdefault(
                    row.candidate_policy_id,
                    {"candidate_for": row.policy_id, "audit_id": row.id},
                )
                relations.setdefault(
                    row.policy_id,
                    {"candidate_id": row.candidate_policy_id, "audit_id": row.id},
                )
    policies = []
    for summary in policy_api.list_policies(control, engine_id):
        detail = policy_api.get_policy(control, engine_id, summary["policyId"])
        policies.append({**_policy_snapshot(detail), **relations.get(detail["policyId"], {})})
    policies.sort(key=lambda item: (item["name"] or "", item["id"] or ""))
    return {
        "gateway": _gateway_policy_snapshot(gateway),
        "engine": {
            **_engine_snapshot(engine),
            "mode": attachment.get("mode"),
        },
        "policies": policies,
    }


def queue_engine_attach(
    db: Session,
    control: Any,
    gateway_id: str,
    request: Any,
) -> dict[str, Any]:
    gateway = _require_managed(control, gateway_id)
    _assert_gateway_ready(gateway)
    _assert_updated_at(
        request.expected_gateway_updated_at,
        gateway.get("updatedAt"),
        "Gateway",
    )
    attachment = gateway.get("policyEngineConfiguration") or {}
    engine = None
    if attachment.get("arn"):
        engine = policy_api.get_policy_engine(
            control,
            engine_id_from_arn(attachment["arn"]),
        )
    requested = request.model_dump(mode="json")
    requested["engine_name"] = request.name or _resource_name(
        f"{gateway['name']}_policy"
    )
    change = _new_change(
        db,
        gateway=gateway,
        engine=engine,
        operation="engine_attach",
        before={
            "gateway": _gateway_policy_snapshot(gateway),
            "engine": _engine_snapshot(engine) if engine else None,
        },
        requested=requested,
        override_reason=request.override_reason,
    )
    return _change_out(change)


def queue_policy_create(
    db: Session,
    control: Any,
    gateway_id: str,
    request: Any,
) -> dict[str, Any]:
    gateway = _require_managed(control, gateway_id)
    _assert_gateway_ready(gateway)
    _assert_updated_at(
        request.expected_gateway_updated_at,
        gateway.get("updatedAt"),
        "Gateway",
    )
    attachment, engine = _attached_engine(control, gateway)
    _assert_shared_for_mutation(
        control,
        attachment["arn"],
        request.acknowledged_gateway_ids,
    )
    change = _new_change(
        db,
        gateway=gateway,
        engine=engine,
        operation="policy_create",
        before={
            "gateway": _gateway_policy_snapshot(gateway),
            "engine": _engine_snapshot(engine),
        },
        requested=request.model_dump(mode="json"),
        # the editor collects a justification for a zero-evidence change; it has
        # to reach the audit column or the audit entry reads "OVERRIDE REASON -"
        override_reason=request.override_reason,
    )
    return _change_out(change)


def queue_policy_update(
    db: Session,
    control: Any,
    gateway_id: str,
    policy_id: str,
    request: Any,
) -> dict[str, Any]:
    gateway = _require_managed(control, gateway_id)
    _assert_gateway_ready(gateway)
    _assert_updated_at(
        request.expected_gateway_updated_at,
        gateway.get("updatedAt"),
        "Gateway",
    )
    attachment, engine = _attached_engine(control, gateway)
    detail = policy_api.get_policy(control, engine["policyEngineId"], policy_id)
    _assert_updated_at(
        request.expected_policy_updated_at,
        detail.get("updatedAt"),
        "Policy",
    )
    _assert_shared_for_mutation(
        control,
        attachment["arn"],
        request.acknowledged_gateway_ids,
    )
    operation = (
        "policy_candidate_create"
        if detail.get("enforcementMode") == "ACTIVE"
        else "policy_update"
    )
    change = _new_change(
        db,
        gateway=gateway,
        engine=engine,
        policy=detail,
        operation=operation,
        before={
            "gateway": _gateway_policy_snapshot(gateway),
            "engine": _engine_snapshot(engine),
            "policy": _policy_snapshot(detail),
        },
        requested=request.model_dump(mode="json"),
        override_reason=request.override_reason,
    )
    return _change_out(change)


def _assert_policy_deletable(
    gateway: dict[str, Any],
    detail: dict[str, Any],
) -> None:
    attachment = gateway.get("policyEngineConfiguration") or {}
    if attachment.get("mode") == "ENFORCE" and detail.get("enforcementMode") == "ACTIVE":
        raise AppError(
            "governance.policy_delete_enforced",
            "Cannot delete an ACTIVE Policy while the Gateway is in ENFORCE mode; "
            "roll the Policy back to LOG_ONLY first",
            status_code=409,
        )


def queue_policy_delete(
    db: Session,
    control: Any,
    gateway_id: str,
    policy_id: str,
    request: Any,
) -> dict[str, Any]:
    gateway = _require_managed(control, gateway_id)
    _assert_gateway_ready(gateway)
    _assert_updated_at(
        request.expected_gateway_updated_at,
        gateway.get("updatedAt"),
        "Gateway",
    )
    attachment, engine = _attached_engine(control, gateway)
    detail = policy_api.get_policy(control, engine["policyEngineId"], policy_id)
    _assert_updated_at(
        request.expected_policy_updated_at,
        detail.get("updatedAt"),
        "Policy",
    )
    _assert_shared_for_mutation(
        control,
        attachment["arn"],
        request.acknowledged_gateway_ids,
    )
    _assert_policy_deletable(gateway, detail)
    change = _new_change(
        db,
        gateway=gateway,
        engine=engine,
        policy=detail,
        operation="policy_delete",
        before={
            "gateway": _gateway_policy_snapshot(gateway),
            "engine": _engine_snapshot(engine),
            "policy": _policy_snapshot(detail),
        },
        requested=request.model_dump(mode="json"),
        override_reason=request.override_reason,
    )
    return _change_out(change)


def _candidate_relation(
    db: Session,
    gateway_id: str,
    policy_id: str,
    audit_id: str | None = None,
) -> PolicyChange | None:
    query = db.query(PolicyChange).filter(
        PolicyChange.gateway_id == gateway_id,
        PolicyChange.candidate_policy_id.is_not(None),
        or_(
            PolicyChange.policy_id == policy_id,
            PolicyChange.candidate_policy_id == policy_id,
        ),
    )
    if audit_id:
        query = query.filter(PolicyChange.id == audit_id)
    return query.order_by(PolicyChange.created_at.desc()).first()


def queue_policy_transition(
    db: Session,
    control: Any,
    gateway_id: str,
    policy_id: str,
    request: Any,
    *,
    rollback: bool,
    evidence_count: int,
) -> dict[str, Any]:
    gateway = _require_managed(control, gateway_id)
    _assert_gateway_ready(gateway)
    _assert_updated_at(
        request.expected_gateway_updated_at,
        gateway.get("updatedAt"),
        "Gateway",
    )
    attachment, engine = _attached_engine(control, gateway)
    _assert_shared_for_mutation(
        control,
        attachment["arn"],
        request.acknowledged_gateway_ids,
    )
    relation = _candidate_relation(db, gateway_id, policy_id, request.audit_id)
    requested = request.model_dump(mode="json")
    requested["evidence_count"] = evidence_count
    policies: dict[str, Any] = {}
    selected = policy_api.get_policy(control, engine["policyEngineId"], policy_id)
    _assert_updated_at(
        request.expected_policy_updated_at,
        selected.get("updatedAt"),
        "Policy",
    )
    policies["selected"] = _policy_snapshot(selected)
    if relation and relation.policy_id and relation.candidate_policy_id:
        original = policy_api.get_policy(
            control,
            engine["policyEngineId"],
            relation.policy_id,
        )
        candidate = policy_api.get_policy(
            control,
            engine["policyEngineId"],
            relation.candidate_policy_id,
        )
        policies.update(
            original=_policy_snapshot(original),
            candidate=_policy_snapshot(candidate),
        )
        requested.update(
            original_policy_id=relation.policy_id,
            candidate_policy_id=relation.candidate_policy_id,
            relation_audit_id=relation.id,
        )
    elif rollback:
        snapshot_query = db.query(PolicyChange).filter(
            PolicyChange.gateway_id == gateway_id,
            PolicyChange.policy_id == policy_id,
            PolicyChange.status.in_(("succeeded", "partial")),
        )
        if request.audit_id:
            snapshot_query = snapshot_query.filter(PolicyChange.id == request.audit_id)
        snapshot = snapshot_query.order_by(PolicyChange.created_at.desc()).first()
        before = (snapshot.before if snapshot else None) or {}
        snapshot_policy = before.get("policy") or (before.get("policies") or {}).get(
            "selected"
        )
        if not snapshot_policy:
            raise AppError(
                "governance.rollback_unavailable",
                "No audited Policy snapshot is available for rollback",
                status_code=409,
            )
        requested["snapshot_policy"] = snapshot_policy
        requested["snapshot_audit_id"] = snapshot.id
    if not rollback:
        _assert_evidence_or_override(gateway, requested)
    change = _new_change(
        db,
        gateway=gateway,
        engine=engine,
        policy=selected,
        operation="policy_rollback" if rollback else "policy_promote",
        before={
            "gateway": _gateway_policy_snapshot(gateway),
            "engine": _engine_snapshot(engine),
            "policies": policies,
        },
        requested=requested,
        override_reason=request.override_reason,
    )
    return _change_out(change)


def queue_gateway_mode(
    db: Session,
    control: Any,
    iam: Any,
    gateway_id: str,
    request: Any,
    *,
    evidence_count: int,
) -> dict[str, Any]:
    gateway = _require_managed(control, gateway_id)
    _assert_gateway_ready(gateway)
    _assert_updated_at(
        request.expected_gateway_updated_at,
        gateway.get("updatedAt"),
        "Gateway",
    )
    attachment, engine = _attached_engine(control, gateway)
    _assert_shared_for_mutation(
        control,
        attachment["arn"],
        request.acknowledged_gateway_ids,
    )
    preflight = iam_preflight(
        iam,
        role_arn=gateway.get("roleArn"),
        engine_arn=engine["policyEngineArn"],
        gateway_arn=gateway["gatewayArn"],
    )
    if preflight["status"] != "pass":
        raise AppError(
            f"governance.iam_preflight_{preflight['status']}",
            "Gateway role cannot be verified for Policy evaluation",
            preflight,
            status_code=409,
        )
    requested = request.model_dump(mode="json")
    requested["evidence_count"] = evidence_count
    if request.mode == "ENFORCE":
        if request.confirmation_name != gateway["name"]:
            raise AppError(
                "governance.confirmation_mismatch",
                "Type the exact Gateway name to enable ENFORCE",
                status_code=409,
            )
        _assert_evidence_or_override(gateway, requested)
    change = _new_change(
        db,
        gateway=gateway,
        engine=engine,
        operation="gateway_mode",
        before={
            "gateway": _gateway_policy_snapshot(gateway),
            "engine": _engine_snapshot(engine),
            "iam_preflight": preflight,
        },
        requested=requested,
        override_reason=request.override_reason,
    )
    return _change_out(change)


def _assert_evidence_or_override(
    gateway: dict[str, Any],
    requested: dict[str, Any],
) -> None:
    if int(requested.get("evidence_count") or 0) > 0:
        return
    if (
        requested.get("confirmation_name") != gateway["name"]
        or not str(requested.get("override_reason") or "").strip()
    ):
        raise AppError(
            "governance.evidence_required",
            "At least one matching LOG_ONLY decision is required",
            {"evidence_count": 0},
            status_code=409,
        )


def _resource_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", value)
    cleaned = re.sub(r"^[^A-Za-z]+", "", cleaned)
    return (cleaned or "launchpad_policy")[:48]


def _preflight_change(
    control: Any,
    change: PolicyChange,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    gateway = _require_managed(control, change.gateway_id)
    _assert_gateway_ready(gateway)
    expected = change.requested.get("expected_gateway_updated_at")
    _assert_updated_at(expected, gateway.get("updatedAt"), "Gateway")
    engine = None
    if change.operation != "engine_attach":
        attachment, engine = _attached_engine(control, gateway)
        _assert_shared_for_mutation(
            control,
            attachment["arn"],
            change.requested.get("acknowledged_gateway_ids") or [],
        )
    return gateway, engine


def _execute_engine_attach(
    control: Any,
    iam: Any,
    change: PolicyChange,
    gateway: dict[str, Any],
) -> dict[str, Any]:
    attachment = gateway.get("policyEngineConfiguration") or {}
    if attachment.get("arn"):
        engine_id = engine_id_from_arn(attachment["arn"])
        engine = policy_api.get_policy_engine(control, engine_id)
        return {
            "adopted": True,
            "gateway": _gateway_policy_snapshot(gateway),
            "engine": _engine_snapshot(engine),
            "mode": attachment.get("mode"),
        }
    engine = policy_api.create_policy_engine(
        control,
        name=change.requested["engine_name"],
        description=f"Policy Engine managed from Launchpad for {gateway['name']}",
        client_token=change.id,
    )
    engine = policy_api.wait_policy_engine_active(
        control,
        engine["policyEngineId"],
    )
    preflight = iam_preflight(
        iam,
        role_arn=gateway.get("roleArn"),
        engine_arn=engine["policyEngineArn"],
        gateway_arn=gateway["gatewayArn"],
    )
    if preflight["status"] != "pass":
        raise AppError(
            f"governance.iam_preflight_{preflight['status']}",
            "Gateway role cannot evaluate the new Policy Engine",
            preflight,
            status_code=409,
        )
    policy_api.update_gateway_policy_configuration(
        control,
        gateway_id=gateway["gatewayId"],
        engine_arn=engine["policyEngineArn"],
        mode="LOG_ONLY",
    )
    settled = policy_api.wait_gateway_ready(control, gateway["gatewayId"])
    return {
        "adopted": False,
        "gateway": _gateway_policy_snapshot(settled),
        "engine": _engine_snapshot(engine),
        "iam_preflight": preflight,
    }


def _execute_policy_create(
    control: Any,
    change: PolicyChange,
    engine: dict[str, Any],
) -> dict[str, Any]:
    created = policy_api.create_policy(
        control,
        engine_id=engine["policyEngineId"],
        name=change.requested["name"],
        statement=change.requested["statement"],
        description=change.requested.get("description"),
        client_token=change.id,
    )
    settled = policy_api.wait_policy_active(
        control,
        engine["policyEngineId"],
        created["policyId"],
    )
    return {"policy": _policy_snapshot(settled)}


def _execute_policy_update(
    control: Any,
    change: PolicyChange,
    engine: dict[str, Any],
) -> dict[str, Any]:
    current = policy_api.get_policy(
        control,
        engine["policyEngineId"],
        change.policy_id,
    )
    expected = change.requested.get("expected_policy_updated_at")
    _assert_updated_at(expected, current.get("updatedAt"), "Policy")
    if change.operation == "policy_candidate_create":
        candidate_name = _resource_name(
            f"{current['name']}_candidate_{change.id[:8]}"
        )
        candidate = policy_api.create_policy(
            control,
            engine_id=engine["policyEngineId"],
            name=candidate_name,
            statement=change.requested["statement"],
            description=change.requested.get("description"),
            client_token=change.id,
        )
        settled = policy_api.wait_policy_active(
            control,
            engine["policyEngineId"],
            candidate["policyId"],
        )
        change.candidate_policy_id = settled["policyId"]
        return {
            "original": _policy_snapshot(current),
            "candidate": _policy_snapshot(settled),
        }
    policy_api.update_policy(
        control,
        engine_id=engine["policyEngineId"],
        policy_id=current["policyId"],
        statement=change.requested["statement"],
        description=change.requested.get("description"),
    )
    settled = policy_api.wait_policy_active(
        control,
        engine["policyEngineId"],
        current["policyId"],
    )
    return {"policy": _policy_snapshot(settled)}


def _execute_policy_delete(
    control: Any,
    change: PolicyChange,
    gateway: dict[str, Any],
    engine: dict[str, Any],
) -> dict[str, Any]:
    try:
        current = policy_api.get_policy(
            control,
            engine["policyEngineId"],
            change.policy_id,
        )
    except control.exceptions.ResourceNotFoundException:
        # idempotent resume: the policy is already gone
        return {"policy": (change.before or {}).get("policy"), "deleted": True}
    _assert_updated_at(
        change.requested.get("expected_policy_updated_at"),
        current.get("updatedAt"),
        "Policy",
    )
    _assert_policy_deletable(gateway, current)
    policy_api.delete_policy(
        control,
        engine_id=engine["policyEngineId"],
        policy_id=current["policyId"],
    )
    policy_api.wait_policy_deleted(
        control,
        engine["policyEngineId"],
        current["policyId"],
    )
    return {"policy": _policy_snapshot(current), "deleted": True}


def _execute_policy_promote(
    control: Any,
    change: PolicyChange,
    gateway: dict[str, Any],
    engine: dict[str, Any],
) -> dict[str, Any]:
    _assert_evidence_or_override(gateway, change.requested)
    candidate_id = change.requested.get("candidate_policy_id")
    original_id = change.requested.get("original_policy_id")
    if not candidate_id or not original_id:
        current = policy_api.get_policy(
            control,
            engine["policyEngineId"],
            change.policy_id,
        )
        _assert_updated_at(
            change.requested.get("expected_policy_updated_at"),
            current.get("updatedAt"),
            "Policy",
        )
        policy_api.update_policy(
            control,
            engine_id=engine["policyEngineId"],
            policy_id=current["policyId"],
            enforcement_mode="ACTIVE",
        )
        settled = policy_api.wait_policy_active(
            control,
            engine["policyEngineId"],
            current["policyId"],
        )
        return {"policy": _policy_snapshot(settled)}

    candidate = policy_api.get_policy(control, engine["policyEngineId"], candidate_id)
    original = policy_api.get_policy(control, engine["policyEngineId"], original_id)
    if candidate.get("enforcementMode") != "ACTIVE":
        policy_api.update_policy(
            control,
            engine_id=engine["policyEngineId"],
            policy_id=candidate_id,
            enforcement_mode="ACTIVE",
        )
        candidate = policy_api.wait_policy_active(
            control,
            engine["policyEngineId"],
            candidate_id,
        )
    try:
        if original.get("enforcementMode") != "LOG_ONLY":
            policy_api.update_policy(
                control,
                engine_id=engine["policyEngineId"],
                policy_id=original_id,
                enforcement_mode="LOG_ONLY",
            )
            original = policy_api.wait_policy_active(
                control,
                engine["policyEngineId"],
                original_id,
            )
    except Exception as exc:
        raise PartialOperation(
            f"Candidate is ACTIVE but original could not move to LOG_ONLY: {exc}",
            {
                "candidate": _policy_snapshot(candidate),
                "original": _policy_snapshot(
                    policy_api.get_policy(
                        control,
                        engine["policyEngineId"],
                        original_id,
                    )
                ),
            },
        ) from exc
    return {
        "candidate": _policy_snapshot(candidate),
        "original": _policy_snapshot(original),
    }


def _execute_policy_rollback(
    control: Any,
    change: PolicyChange,
    engine: dict[str, Any],
) -> dict[str, Any]:
    candidate_id = change.requested.get("candidate_policy_id")
    original_id = change.requested.get("original_policy_id")
    if candidate_id and original_id:
        original = policy_api.get_policy(control, engine["policyEngineId"], original_id)
        if original.get("enforcementMode") != "ACTIVE":
            policy_api.update_policy(
                control,
                engine_id=engine["policyEngineId"],
                policy_id=original_id,
                enforcement_mode="ACTIVE",
            )
            original = policy_api.wait_policy_active(
                control,
                engine["policyEngineId"],
                original_id,
            )
        candidate = policy_api.get_policy(
            control,
            engine["policyEngineId"],
            candidate_id,
        )
        if candidate.get("enforcementMode") != "LOG_ONLY":
            policy_api.update_policy(
                control,
                engine_id=engine["policyEngineId"],
                policy_id=candidate_id,
                enforcement_mode="LOG_ONLY",
            )
            candidate = policy_api.wait_policy_active(
                control,
                engine["policyEngineId"],
                candidate_id,
            )
        return {
            "original": _policy_snapshot(original),
            "candidate": _policy_snapshot(candidate),
        }
    snapshot = change.requested["snapshot_policy"]
    current = policy_api.get_policy(
        control,
        engine["policyEngineId"],
        change.policy_id,
    )
    _assert_updated_at(
        change.requested.get("expected_policy_updated_at"),
        current.get("updatedAt"),
        "Policy",
    )
    policy_api.update_policy(
        control,
        engine_id=engine["policyEngineId"],
        policy_id=current["policyId"],
        statement=snapshot["statement"],
        enforcement_mode=snapshot["enforcement_mode"],
        description=snapshot.get("description"),
    )
    settled = policy_api.wait_policy_active(
        control,
        engine["policyEngineId"],
        current["policyId"],
    )
    return {"policy": _policy_snapshot(settled)}


def _execute_gateway_mode(
    control: Any,
    iam: Any,
    change: PolicyChange,
    gateway: dict[str, Any],
    engine: dict[str, Any],
) -> dict[str, Any]:
    requested_mode = change.requested["mode"]
    if requested_mode == "ENFORCE":
        _assert_evidence_or_override(gateway, change.requested)
    preflight = iam_preflight(
        iam,
        role_arn=gateway.get("roleArn"),
        engine_arn=engine["policyEngineArn"],
        gateway_arn=gateway["gatewayArn"],
    )
    if preflight["status"] != "pass":
        raise AppError(
            f"governance.iam_preflight_{preflight['status']}",
            "Gateway role cannot be verified for Policy evaluation",
            preflight,
            status_code=409,
        )
    current = gateway.get("policyEngineConfiguration") or {}
    if current.get("mode") != requested_mode:
        policy_api.update_gateway_policy_configuration(
            control,
            gateway_id=gateway["gatewayId"],
            engine_arn=engine["policyEngineArn"],
            mode=requested_mode,
        )
        gateway = policy_api.wait_gateway_ready(control, gateway["gatewayId"])
    return {
        "gateway": _gateway_policy_snapshot(gateway),
        "engine": _engine_snapshot(engine),
        "iam_preflight": preflight,
    }


def run_policy_change(
    change_id: str,
    *,
    control: Any | None = None,
    iam: Any | None = None,
) -> None:
    control = control or control_client()
    iam = iam or iam_client()
    db = SessionLocal()
    try:
        change = db.get(PolicyChange, change_id)
        if change is None or change.status not in ("pending", "running"):
            return
        change.status = "running"
        change.started_at = change.started_at or datetime.now(UTC)
        db.commit()
        try:
            gateway, engine = _preflight_change(control, change)
            if change.operation == "engine_attach":
                after = _execute_engine_attach(control, iam, change, gateway)
            elif change.operation == "policy_create":
                after = _execute_policy_create(control, change, engine)
            elif change.operation in ("policy_update", "policy_candidate_create"):
                after = _execute_policy_update(control, change, engine)
            elif change.operation == "policy_delete":
                after = _execute_policy_delete(control, change, gateway, engine)
            elif change.operation == "policy_promote":
                after = _execute_policy_promote(control, change, gateway, engine)
            elif change.operation == "policy_rollback":
                after = _execute_policy_rollback(control, change, engine)
            elif change.operation == "gateway_mode":
                after = _execute_gateway_mode(control, iam, change, gateway, engine)
            else:
                raise RuntimeError(f"unknown Policy operation {change.operation}")
            change.after = json_snapshot(after)
            change.status = "succeeded"
        except PartialOperation as exc:
            change.after = json_snapshot(exc.after)
            change.status = "partial"
            change.error = str(exc)[:2000]
        except Exception as exc:
            change.status = "failed"
            change.error = f"{type(exc).__name__}: {exc}"[:2000]
        change.completed_at = datetime.now(UTC)
        db.commit()
        invalidate_gateway_cache()
    finally:
        db.close()


def start_generation(
    db: Session,
    control: Any,
    gateway_id: str,
    request: Any,
) -> dict[str, Any]:
    gateway = _require_managed(control, gateway_id)
    _assert_gateway_ready(gateway)
    _assert_updated_at(
        request.expected_gateway_updated_at,
        gateway.get("updatedAt"),
        "Gateway",
    )
    attachment, engine = _attached_engine(control, gateway)
    _assert_shared_for_mutation(
        control,
        attachment["arn"],
        request.acknowledged_gateway_ids,
    )
    change = _new_change(
        db,
        gateway=gateway,
        engine=engine,
        operation="policy_generation",
        before={
            "gateway": _gateway_policy_snapshot(gateway),
            "engine": _engine_snapshot(engine),
        },
        requested=request.model_dump(mode="json"),
        override_reason=request.override_reason,
    )
    change.status = "running"
    change.started_at = datetime.now(UTC)
    db.commit()
    try:
        generation = policy_api.start_policy_generation(
            control,
            engine_id=engine["policyEngineId"],
            gateway_arn=gateway["gatewayArn"],
            name=request.name,
            text=request.text,
            client_token=change.id,
        )
        change.after = json_snapshot(
            {
                "generation_id": generation.get("policyGenerationId"),
                "status": generation.get("status"),
            }
        )
        change.status = "succeeded"
        return {
            "id": generation.get("policyGenerationId"),
            "status": generation.get("status"),
            "status_reasons": generation.get("statusReasons") or [],
            "findings": generation.get("findings"),
            "assets": [],
            "operation": _change_out(change),
        }
    except Exception as exc:
        change.status = "failed"
        change.error = f"{type(exc).__name__}: {exc}"[:2000]
        raise
    finally:
        change.completed_at = datetime.now(UTC)
        db.commit()


def generation_view(
    control: Any,
    gateway_id: str,
    generation_id: str,
) -> dict[str, Any]:
    gateway = _require_gateway(control, gateway_id)
    _, engine = _attached_engine(control, gateway)
    generation = policy_api.get_policy_generation(
        control,
        engine["policyEngineId"],
        generation_id,
    )
    assets = []
    if generation.get("status") == "GENERATED":
        assets = policy_api.list_policy_generation_assets(
            control,
            engine["policyEngineId"],
            generation_id,
        )
    return {
        "id": generation.get("policyGenerationId"),
        "status": generation.get("status"),
        "status_reasons": generation.get("statusReasons") or [],
        "findings": generation.get("findings"),
        "assets": [
            {
                "id": asset.get("policyGenerationAssetId"),
                "statement": _statement(asset),
                "findings": asset.get("findings"),
                "raw_text_fragment": asset.get("rawTextFragment"),
            }
            for asset in assets
        ],
    }


def reconcile_policy_changes(control: Any | None = None) -> list[str]:
    """Reconcile interrupted operations without replaying AWS mutations."""
    db = SessionLocal()
    reconciled: list[str] = []
    try:
        rows = (
            db.query(PolicyChange)
            .filter(PolicyChange.status.in_(("pending", "running")))
            .all()
        )
        if not rows:
            return reconciled
        control = control or control_client()
        for change in rows:
            try:
                if _change_matches_live(control, change):
                    change.status = "succeeded"
                elif _change_is_conservative_partial(control, change):
                    change.status = "partial"
                else:
                    change.status = "interrupted"
            except Exception as exc:
                change.status = "interrupted"
                change.error = f"reconciliation failed: {type(exc).__name__}: {exc}"[:2000]
            change.completed_at = datetime.now(UTC)
            reconciled.append(change.id)
        db.commit()
        return reconciled
    finally:
        db.close()


def _change_matches_live(control: Any, change: PolicyChange) -> bool:
    gateway = policy_api.get_gateway(control, change.gateway_id)
    if change.operation == "gateway_mode":
        return (gateway.get("policyEngineConfiguration") or {}).get("mode") == (
            change.requested.get("mode")
        )
    after = change.after or {}
    policy_snapshot = after.get("policy")
    if policy_snapshot and policy_snapshot.get("id"):
        engine_id = change.engine_id or engine_id_from_arn(change.engine_arn)
        live = policy_api.get_policy(control, engine_id, policy_snapshot["id"])
        return (
            _statement(live) == policy_snapshot.get("statement")
            and live.get("enforcementMode") == policy_snapshot.get("enforcement_mode")
        )
    if change.operation == "engine_attach" and change.engine_arn:
        return (gateway.get("policyEngineConfiguration") or {}).get("arn") == (
            change.engine_arn
        )
    return False


def _change_is_conservative_partial(control: Any, change: PolicyChange) -> bool:
    candidate_id = change.candidate_policy_id or change.requested.get(
        "candidate_policy_id"
    )
    original_id = change.policy_id or change.requested.get("original_policy_id")
    engine_id = change.engine_id or engine_id_from_arn(change.engine_arn)
    if change.operation != "policy_promote" or not all(
        (candidate_id, original_id, engine_id)
    ):
        return False
    candidate = policy_api.get_policy(control, engine_id, candidate_id)
    original = policy_api.get_policy(control, engine_id, original_id)
    return (
        candidate.get("enforcementMode") == "ACTIVE"
        and original.get("enforcementMode") == "ACTIVE"
    )
