"""Product rules for existing Gateway governance (Gateway lifecycle, rate limits,
target synchronization — Cedar Policy does not exist in the CN build)."""

import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.services.agentcore import gateway as gateway_api
from app.services.workspace import WorkspaceContext

_GATEWAY_CACHE_TTL_S = 30.0
_gateway_cache: dict[str, Any] = {"key": None, "at": 0.0, "data": None}


def iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def json_snapshot(value: Any) -> Any:
    """Return a JSON-safe snapshot for the immutable audit journal."""
    return json.loads(json.dumps(value, default=iso))


def invalidate_gateway_cache() -> None:
    _gateway_cache.update(key=None, at=0.0, data=None)


def _attachability(gateway: dict[str, Any]) -> dict[str, Any]:
    """The CN gateway is AWS_IAM (N3) and NONE is attachable too; a CUSTOM_JWT authorizer
    has no Launchpad-managed credential (no runtime honours an OAuth gateway and the
    bootstrap writes no OAuth provider), so it stays catalog-only."""
    authorizer = gateway.get("authorizerType")
    if authorizer == "AWS_IAM":
        return {"attachable": True, "reason": None, "auth_type": "aws_iam"}
    if authorizer == "NONE":
        return {"attachable": True, "reason": None, "auth_type": "none"}
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
    for summary in gateway_api.list_gateways(control):
        if summary.get("protocolType") not in (None, "MCP"):
            continue
        detail = gateway_api.get_gateway(control, summary["gatewayId"])
        if detail.get("protocolType") == "MCP":
            details.append(detail)
    return details


def _gateway_summary(
    control: Any,
    gateway: dict[str, Any],
    workspace: WorkspaceContext,
) -> dict[str, Any]:
    tags = gateway_api.list_tags(control, gateway["gatewayArn"])
    targets = gateway_api.list_gateway_targets(control, gateway["gatewayId"])
    configured_gateway_id = workspace.resources.get("gateway_id")
    configured_gateway_url = workspace.resources.get("gateway_url")
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
        "managed": gateway_api.is_managed(tags),
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
        "attachability": _attachability(gateway),
        "policy_test_available": bool(
            configured_gateway_id
            and configured_gateway_url
            and gateway["gatewayId"] == configured_gateway_id
            and gateway.get("gatewayUrl") == configured_gateway_url
        ),
    }


def list_gateway_views(
    control: Any,
    workspace: WorkspaceContext,
    *,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    cache_key = (id(control), workspace.id, workspace.region, workspace.account_id)
    if (
        not refresh
        and _gateway_cache["data"] is not None
        and _gateway_cache["key"] == cache_key
        and time.monotonic() - _gateway_cache["at"] < _GATEWAY_CACHE_TTL_S
    ):
        return _gateway_cache["data"]

    details = _all_mcp_gateway_details(control)
    views = [_gateway_summary(control, detail, workspace) for detail in details]
    views.sort(key=lambda item: (not item["managed"], item["name"], item["id"]))
    _gateway_cache.update(key=cache_key, at=time.monotonic(), data=views)
    return views


def _require_gateway(control: Any, gateway_id: str) -> dict[str, Any]:
    try:
        gateway = gateway_api.get_gateway(control, gateway_id)
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


def require_gateway(control: Any, gateway_id: str) -> dict[str, Any]:
    """The live MCP Gateway ``gateway_id`` (``GetGateway``), or the governance 404 /
    409 the console already speaks — shared with the registry's gateway import
    (WB-005) so both surfaces resolve a Gateway identically."""
    return _require_gateway(control, gateway_id)


def _require_managed(control: Any, gateway_id: str) -> dict[str, Any]:
    gateway = _require_gateway(control, gateway_id)
    tags = gateway_api.list_tags(control, gateway["gatewayArn"])
    if not gateway_api.is_managed(tags):
        raise AppError(
            "governance.gateway_not_managed",
            "Manage the Gateway before changing its rate limits or targets",
            status_code=409,
        )
    return gateway


def manage_gateway(control: Any, gateway_id: str) -> dict[str, Any]:
    gateway = _require_gateway(control, gateway_id)
    gateway_api.tag_managed(control, gateway["gatewayArn"])
    invalidate_gateway_cache()
    return {"gateway_id": gateway_id, "managed": True}


def unmanage_gateway(control: Any, gateway_id: str) -> dict[str, Any]:
    gateway = _require_gateway(control, gateway_id)
    gateway_api.untag_managed(control, gateway["gatewayArn"])
    invalidate_gateway_cache()
    return {"gateway_id": gateway_id, "managed": False}


# ---- Gateway rate limits (synchronous control-plane calls) --------------------

# Fixed keys from the AgentCore dimension catalogue; `$.context.jwt.<claim>` is the
# only open-ended family and is matched by RATE_LIMIT_JWT_CLAIM below.
RATE_LIMIT_FIXED_KEYS = (
    "targetName",
    "toolName",
    "qualifiedModelId",
    "$.context.iam.principal",
    "$.context.iam.sourceIdentity",
)
RATE_LIMIT_JWT_PREFIX = "$.context.jwt."
# botocore's DimensionKey pattern for the claim part (a 1-char claim is rejected by AWS too)
RATE_LIMIT_JWT_CLAIM = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_\-.]{0,61}[a-zA-Z0-9_]$")
# metric → periods the service accepts (tokens are per-minute only; connections per-second only)
RATE_LIMIT_PERIODS: dict[str, tuple[str, ...]] = {
    "requests": ("second", "minute"),
    "tokens": ("minute",),
    "connections": ("second",),
}
RATE_LIMIT_MAX_KEYS = 10
RATE_LIMIT_MAX_ENTRIES = 1000
RATE_LIMIT_MAX_RATE = 10_000_000
RATE_LIMIT_MAX_DESCRIPTION = 512
RATE_LIMIT_WILDCARD = "*"


def _rate_limit_invalid(reason: str, message: str, **extra: Any) -> AppError:
    return AppError(
        "governance.rate_limit_invalid",
        message,
        {"reason": reason, **extra},
        status_code=422,
    )


def is_rate_limit_dimension_key(key: str) -> bool:
    if key in RATE_LIMIT_FIXED_KEYS:
        return True
    if key.startswith(RATE_LIMIT_JWT_PREFIX):
        return bool(RATE_LIMIT_JWT_CLAIM.match(key[len(RATE_LIMIT_JWT_PREFIX) :]))
    return False


def _entry_field(entry: Any, name: str) -> Any:
    """Read a field from a pydantic entry or a plain dict alike."""
    return entry.get(name) if isinstance(entry, dict) else getattr(entry, name, None)


def _rate_configs(entry: Any, metric: str) -> list[dict[str, Any]]:
    return [
        item if isinstance(item, dict) else {"rate": item.rate, "period": item.period}
        for item in (_entry_field(entry, metric) or [])
    ]


def _entry_dimensions(entry: Any) -> dict[str, str]:
    return dict(_entry_field(entry, "dimensions") or {})


def validate_rate_limit_spec(
    dimension_keys: list[str],
    entries: list[Any],
    description: str | None,
) -> list[dict[str, Any]]:
    """Check a rate-limit spec against the documented service rules.

    Runs before any AWS call so a malformed spec answers 422 with a stable
    ``detail.reason`` instead of a ``ValidationException`` that names the
    operation. Returns the ``entries`` in the exact ``LimitEntry`` wire shape
    (only the metrics that were supplied are present).
    """
    if not 1 <= len(dimension_keys) <= RATE_LIMIT_MAX_KEYS:
        raise _rate_limit_invalid(
            "dimension_keys_count",
            f"a rate limit needs 1-{RATE_LIMIT_MAX_KEYS} dimension keys",
        )
    seen: set[str] = set()
    for key in dimension_keys:
        if not is_rate_limit_dimension_key(key):
            raise _rate_limit_invalid(
                "dimension_key_unknown",
                f"{key!r} is not a Gateway rate-limit dimension key",
                key=key,
            )
        if key in seen:
            raise _rate_limit_invalid(
                "dimension_key_duplicate", f"dimension key {key!r} is listed twice", key=key
            )
        seen.add(key)
    if not 1 <= len(entries) <= RATE_LIMIT_MAX_ENTRIES:
        raise _rate_limit_invalid(
            "entries_count", f"a rate limit needs 1-{RATE_LIMIT_MAX_ENTRIES} entries"
        )
    if description is not None and len(description) > RATE_LIMIT_MAX_DESCRIPTION:
        raise _rate_limit_invalid(
            "description_too_long",
            f"description must be at most {RATE_LIMIT_MAX_DESCRIPTION} characters",
        )

    wire: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        dimensions = _entry_dimensions(entry)
        if set(dimensions) != set(dimension_keys):
            raise _rate_limit_invalid(
                "entry_dimensions_mismatch",
                f"entry {index} must set exactly the dimension keys {list(dimension_keys)}",
                index=index,
            )
        # `*` only in trailing positions: once a value is the wildcard, every later
        # (less significant) key must be the wildcard as well.
        values = [dimensions[key] for key in dimension_keys]
        if any(not value for value in values):
            raise _rate_limit_invalid(
                "entry_dimension_empty", f"entry {index} has an empty dimension value", index=index
            )
        wildcard_seen = False
        for value in values:
            if value == RATE_LIMIT_WILDCARD:
                wildcard_seen = True
            elif wildcard_seen:
                raise _rate_limit_invalid(
                    "wildcard_not_trailing",
                    f"entry {index}: '*' is only allowed in trailing dimension positions",
                    index=index,
                )
        wire_entry: dict[str, Any] = {"dimensions": dict(dimensions)}
        for metric, periods in RATE_LIMIT_PERIODS.items():
            configs = _rate_configs(entry, metric)
            if not configs:
                continue
            if len(configs) != 1:
                raise _rate_limit_invalid(
                    "rate_config_count",
                    f"entry {index}: {metric} takes exactly one rate config",
                    index=index,
                    metric=metric,
                )
            config = configs[0]
            rate = config.get("rate")
            if not isinstance(rate, int | float) or not 0 <= rate <= RATE_LIMIT_MAX_RATE:
                raise _rate_limit_invalid(
                    "rate_out_of_range",
                    f"entry {index}: {metric} rate must be between 0 and {RATE_LIMIT_MAX_RATE}",
                    index=index,
                    metric=metric,
                )
            if config.get("period") not in periods:
                raise _rate_limit_invalid(
                    "period_not_allowed",
                    f"entry {index}: {metric} accepts period {' or '.join(periods)}",
                    index=index,
                    metric=metric,
                )
            wire_entry[metric] = [{"rate": float(rate), "period": config["period"]}]
        if len(wire_entry) == 1:
            raise _rate_limit_invalid(
                "entry_no_metric",
                f"entry {index} needs at least one of requests, tokens or connections",
                index=index,
            )
        wire.append(wire_entry)
    return wire


def _rate_limit_out(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("rateLimitId"),
        "gateway_id": item.get("gatewayIdentifier"),
        "description": item.get("description") or "",
        "dimension_keys": list(item.get("dimensionKeys") or []),
        "entries": list(item.get("entries") or []),
        "status": item.get("status") or "",
        "created_at": iso(item.get("createdAt")),
        "updated_at": iso(item.get("updatedAt")),
    }


def _aws_payload(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    return {key: value for key, value in response.items() if key != "ResponseMetadata"}


def list_rate_limits(control: Any, gateway_id: str) -> dict[str, Any]:
    """Read-only: every rate limit on a Gateway, managed or not."""
    return {
        "rate_limits": [
            _rate_limit_out(item)
            for item in gateway_api.list_gateway_rate_limits(control, gateway_id)
        ]
    }


def _current_rate_limit(control: Any, gateway_id: str, rate_limit_id: str) -> dict[str, Any]:
    """The prior rate limit for the ``before`` snapshot, or ``{}`` if unreadable.

    A missing rate limit surfaces from the mutation call itself as the mapped
    AWS error, not from this read.
    """
    try:
        return _aws_payload(gateway_api.get_gateway_rate_limit(control, gateway_id, rate_limit_id))
    except Exception:
        return {}


def _journaled_control_call(
    db: Session,
    *,
    workspace_id: str,
    gateway: dict[str, Any],
    operation: str,
    before: dict[str, Any],
    requested: dict[str, Any],
    call: Callable[[], Any],
) -> dict[str, Any]:
    """Run one synchronous Gateway control-plane mutation and return its payload.

    Used to also open/close a ``policy_changes`` audit row around the call; Cedar
    Policy and its journal do not exist in the CN build, so this reduces to a
    direct call. The signature is unchanged so every caller (the rate-limit
    mutations and target synchronize) stays identical — ``db``/``workspace_id``/
    ``gateway``/``operation``/``before``/``requested`` are accepted but unused.
    """
    return _aws_payload(call())


def create_rate_limit(
    db: Session,
    control: Any,
    workspace_id: str,
    gateway_id: str,
    request: Any,
) -> dict[str, Any]:
    entries = validate_rate_limit_spec(
        list(request.dimension_keys), list(request.entries), request.description
    )
    gateway = _require_managed(control, gateway_id)
    requested = {
        "dimension_keys": list(request.dimension_keys),
        "entries": entries,
        "description": request.description,
    }
    response = _journaled_control_call(
        db,
        workspace_id=workspace_id,
        gateway=gateway,
        operation="rate_limit.create",
        before={},
        requested=requested,
        call=lambda: gateway_api.create_gateway_rate_limit(
            control,
            gateway_id=gateway_id,
            dimension_keys=list(request.dimension_keys),
            entries=entries,
            description=request.description,
            client_token=uuid.uuid4().hex,
        ),
    )
    return _rate_limit_out(response)


def update_rate_limit(
    db: Session,
    control: Any,
    workspace_id: str,
    gateway_id: str,
    rate_limit_id: str,
    request: Any,
) -> dict[str, Any]:
    if getattr(request, "dimension_keys", None) is not None:
        raise _rate_limit_invalid(
            "dimension_keys_immutable",
            "dimension keys cannot change after creation — delete and recreate the rate limit",
        )
    gateway = _require_managed(control, gateway_id)
    before = _current_rate_limit(control, gateway_id, rate_limit_id)
    dimension_keys = list(before.get("dimensionKeys") or [])
    if not dimension_keys:
        # The prior record is unreadable (or the stub has none): derive the key set
        # from the first entry so the trailing-`*` and period rules still run.
        dimension_keys = list(_entry_dimensions(request.entries[0])) if request.entries else []
    entries = validate_rate_limit_spec(dimension_keys, list(request.entries), request.description)
    requested = {"entries": entries, "description": request.description}
    response = _journaled_control_call(
        db,
        workspace_id=workspace_id,
        gateway=gateway,
        operation="rate_limit.update",
        before=before,
        requested=requested,
        call=lambda: gateway_api.update_gateway_rate_limit(
            control,
            gateway_id=gateway_id,
            rate_limit_id=rate_limit_id,
            entries=entries,
            description=request.description,
        ),
    )
    return _rate_limit_out(response)


def delete_rate_limit(
    db: Session,
    control: Any,
    workspace_id: str,
    gateway_id: str,
    rate_limit_id: str,
) -> dict[str, Any]:
    gateway = _require_managed(control, gateway_id)
    before = _current_rate_limit(control, gateway_id, rate_limit_id)
    response = _journaled_control_call(
        db,
        workspace_id=workspace_id,
        gateway=gateway,
        operation="rate_limit.delete",
        before=before,
        requested={"rate_limit_id": rate_limit_id},
        call=lambda: gateway_api.delete_gateway_rate_limit(
            control, gateway_id=gateway_id, rate_limit_id=rate_limit_id
        ),
    )
    return {
        "deleted": True,
        "id": response.get("rateLimitId") or rate_limit_id,
        "status": response.get("status") or "DELETING",
    }


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


def external_tools_list_command(
    gateway: dict[str, Any],
    workspace: WorkspaceContext,
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
    if gateway.get("gatewayId") == workspace.resources.get("gateway_id"):
        return None
    return (
        f"curl -sS -X POST '{url}' \\\n"
        '  -H "Authorization: Bearer $GATEWAY_ACCESS_TOKEN" \\\n'
        "  -H 'Content-Type: application/json' \\\n"
        "  -H 'Accept: application/json, text/event-stream' \\\n"
        '  -d \'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\''
    )


# Target statuses SynchronizeGatewayTargets accepts. CREATING/UPDATING/DELETING are
# transient, SYNCHRONIZING is already in flight, and the three *_PENDING_AUTH
# states are refused by the service until the operator completes authorization.
SYNCHRONIZABLE_TARGET_STATUSES = frozenset(
    {"READY", "SYNCHRONIZE_UNSUCCESSFUL", "UPDATE_UNSUCCESSFUL", "FAILED"}
)
PENDING_AUTH_TARGET_STATUSES = frozenset(
    {"CREATE_PENDING_AUTH", "UPDATE_PENDING_AUTH", "SYNCHRONIZE_PENDING_AUTH"}
)


def _mcp_server_config(target: dict[str, Any]) -> dict[str, Any] | None:
    mcp = (target.get("targetConfiguration") or {}).get("mcp") or {}
    server = mcp.get("mcpServer")
    return server if isinstance(server, dict) else None


def target_sync_gate(target: dict[str, Any]) -> str | None:
    """Why SynchronizeGatewayTargets would refuse this target, or ``None``.

    Mirrors the documented service rules so the console never re-derives them:
    only MCP-server targets sync (Lambda/OpenAPI/Smithy/connector schemas are
    static by construction), a static ``mcpToolSchema`` disables sync, and the
    target must be settled (``READY`` or one of the unsuccessful/failed states).
    """
    server = _mcp_server_config(target)
    if server is None:
        return "not_mcp_server"
    if server.get("mcpToolSchema") is not None:
        return "static_tool_schema"
    status = (target.get("status") or "").upper()
    if status in PENDING_AUTH_TARGET_STATUSES:
        return "pending_auth"
    if status == "SYNCHRONIZING":
        return "synchronizing"
    if status not in SYNCHRONIZABLE_TARGET_STATUSES:
        return "not_ready"
    return None


TARGET_PROTOCOLS = ("mcp", "http", "inference")
"""``TargetConfiguration`` union members (bedrock-agentcore-control 2023-06-05).

Each protocol is itself a union: ``mcp{openApiSchema, smithyModel, lambda,
mcpServer, apiGateway, connector}``, ``http{agentcoreRuntime, passthrough,
connector}``, ``inference{connector, provider}``. Only the protocol list is pinned
here — the variant is whatever single key AWS set, so a future union member
surfaces as ``variant=<key>`` instead of raising.
"""


def target_kind(target: dict[str, Any]) -> dict[str, Any]:
    """``{"protocol": "mcp"|"http"|"inference"|"unknown", "variant": <union key>|None}``.

    Pure projection of ``targetConfiguration``; tolerant by design — an empty or
    unrecognized configuration maps to ``{"protocol": "unknown", "variant": None}``
    and an unrecognized top-level key to ``{"protocol": <key>, "variant": None}``.
    """
    configuration = target.get("targetConfiguration")
    if not isinstance(configuration, dict) or not configuration:
        return {"protocol": "unknown", "variant": None}
    protocol = next((key for key in TARGET_PROTOCOLS if key in configuration), None)
    if protocol is None:
        protocol = next(iter(configuration))
        return {"protocol": str(protocol), "variant": None}
    members = configuration.get(protocol)
    variant = None
    if isinstance(members, dict):
        variant = next((str(key) for key, value in members.items() if value is not None), None)
    return {"protocol": protocol, "variant": variant}


def _target_projection(target: dict[str, Any]) -> dict[str, Any]:
    """The console's view of one GatewayTarget (detail + sync response)."""
    server = _mcp_server_config(target) or {}
    reason = target_sync_gate(target)
    return {
        "id": target.get("targetId"),
        "name": target.get("name"),
        "status": target.get("status"),
        "status_reasons": target.get("statusReasons") or [],
        "description": target.get("description") or "",
        "kind": target_kind(target),
        "listing_mode": server.get("listingMode"),
        "last_synchronized_at": iso(target.get("lastSynchronizedAt")),
        "synchronizable": reason is None,
        "not_synchronizable_reason": reason,
    }


def actions_uncovered_targets(targets: list[dict[str, Any]]) -> list[str]:
    """Names of targets whose protocol carries no tool schema (``http`` / ``inference``).

    ``discover_actions`` only reads MCP schemas, so these targets legitimately
    contribute zero actions; the detail surfaces them so an empty ACTIONS cell is
    not misread as a discovery failure.
    """
    return [
        str(target.get("name") or target.get("targetId") or "")
        for target in targets
        if target_kind(target)["protocol"] in ("http", "inference")
    ]


def synchronize_target(
    db: Session,
    control: Any,
    workspace_id: str,
    gateway_id: str,
    target_id: str,
) -> dict[str, Any]:
    """Re-fetch a dynamic MCP-server target's tool list (SynchronizeGatewayTargets).

    Gates run before any mutating AWS call: the Gateway must be managed and the
    target must be synchronizable (``409 governance.target_not_synchronizable``
    with ``detail.reason``). AWS ``ConflictException`` reaches the client as
    ``409 aws.conflict``.
    """
    gateway = _require_managed(control, gateway_id)
    # A missing target surfaces as 404 through the shared ClientError envelope.
    current = _aws_payload(gateway_api.get_gateway_target(control, gateway_id, target_id))
    reason = target_sync_gate(current)
    if reason is not None:
        raise AppError(
            "governance.target_not_synchronizable",
            f"Target {current.get('name') or target_id} cannot be synchronized ({reason})",
            status_code=409,
            detail={"reason": reason, "target_id": target_id, "status": current.get("status")},
        )
    response = _journaled_control_call(
        db,
        workspace_id=workspace_id,
        gateway=gateway,
        operation="target.synchronize",
        before=_target_projection(current),
        requested={"target_id": target_id, "target_name": current.get("name")},
        call=lambda: gateway_api.synchronize_gateway_target(control, gateway_id, target_id),
    )
    # The 202 body may lag the accepted state; fall back to the pre-call fields.
    return _target_projection({**current, **response})


def gateway_detail(
    control: Any,
    gateway_id: str,
    workspace: WorkspaceContext,
) -> dict[str, Any]:
    gateway = _require_gateway(control, gateway_id)
    summary = _gateway_summary(control, gateway, workspace)
    targets = gateway_api.list_gateway_target_details(control, gateway_id)
    return {
        **summary,
        "authorizer_configuration": gateway.get("authorizerConfiguration"),
        "protocol_configuration": gateway.get("protocolConfiguration"),
        "targets": [_target_projection(target) for target in targets],
        "actions": discover_actions(targets),
        "actions_uncovered_targets": actions_uncovered_targets(targets),
        "external_tools_list_command": external_tools_list_command(gateway, workspace),
    }


def assert_managed_gateway(control: Any, gateway_id: str) -> dict[str, Any]:
    return _require_managed(control, gateway_id)

