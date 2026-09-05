"""Platform-level registry orchestration.

The ledger stays the operational source of truth; the registry is the catalog.
Sync direction is platform → registry, and ledger rows record their
registryRecordId.
"""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime
from typing import Any

from app.core.config import REPO_ROOT
from app.core.errors import AppError
from app.models.ledger import Agent
from app.services import mcp_client
from app.services.agentcore import policy as policy_api
from app.services.agentcore import registry as reg
from app.services.agentcore import runtime as rt
from app.services.agentcore.client import (
    control_client,
    data_client,
    registry_control_client,
    registry_data_client,
)
from app.services.skill_ingest import (
    SKILL_MD_MAX_BYTES,
    SKILL_NAME_RE,
    SkillBundle,
    SkillValidationError,
    bundle_from_inline,
    bundle_from_source,
    parse_frontmatter,
    validate_bundle,
)
from app.services.workspace import WorkspaceContext

# Shared frontmatter parser lives in skill_ingest now; kept as a module-level
# name for the existing callers (upload_skill_bundle) and tests.
_parse_frontmatter = parse_frontmatter

SKILLS_DIR = REPO_ROOT / "samples" / "skills"
SKILL_NAME = "expense-report-writer"
GATEWAY_SCOPE = "launchpad-gw/invoke"


class RegistryUnavailableError(AppError):
    """Registry is an optional capability and was disabled during bootstrap."""

    def __init__(self, reason: str | None = None):
        message = reason or "Agent Registry is not configured"
        super().__init__(
            "registry.unavailable",
            message,
            detail={"reason": message},
            status_code=503,
        )


def _registry_id(workspace: WorkspaceContext) -> str:
    resources = workspace.resources
    registry_id = resources.get("registry_id")
    if not registry_id:
        raise RegistryUnavailableError(resources.get("registry_unavailable_reason"))
    return registry_id


def _artifacts_bucket(workspace: WorkspaceContext) -> str:
    bucket = workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    return str(bucket)


def register_agent_record(
    agent: Agent, workspace: WorkspaceContext, auto_submit: bool = True
) -> dict[str, Any]:
    """Create/refresh the A2A record for a deployed agent; auto-submit new records."""
    client = registry_control_client(workspace)
    registry_id = _registry_id(workspace)
    spec = agent.spec or {}
    # A2A-protocol runtimes serve real JSON-RPC at the data-plane URL — their
    # card is directly consumable. HTTP runtime agents get the same URL as an
    # informative endpoint (platform-invoke transport); harness agents keep
    # their harness ARN (the /runtimes/ data plane doesn't apply to them).
    is_a2a = spec.get("protocol") == "a2a"
    arn = agent.arn or ""
    card = reg.build_a2a_card(
        name=agent.name,
        description=(spec.get("system_prompt") or "")[:180] or f"Launchpad agent {agent.name}",
        arn=arn,
        version=agent.version or "1",
        method=agent.method,
        url=(
            reg.data_plane_invocations_url(arn, workspace.region)
            if ":runtime/" in arn else None
        ),
        skills=reg.derive_card_skills(spec) or None,
        transport="a2a-jsonrpc" if is_a2a else "agentcore-http",
    )
    record, created = reg.upsert_record(
        client,
        registry_id,
        name=agent.name,
        description=f"Launchpad agent · method {agent.method}",
        descriptor_type="A2A",
        descriptors=reg.build_a2a_descriptors(card),
    )
    record_id = record["recordId"]
    if created and auto_submit:
        reg.wait_record_settled(client, registry_id, record_id)
        reg.submit_record(client, registry_id, record_id)
    return {"record_id": record_id, "created": created}


def upload_skill_bundle(
    workspace: WorkspaceContext, skill_name: str = SKILL_NAME
) -> dict[str, Any]:
    """Upload the sample skill bundle to S3; return definition metadata."""
    bucket = _artifacts_bucket(workspace)
    skill_dir = SKILLS_DIR / skill_name
    s3 = workspace.client("s3")
    files: list[str] = []
    for path in sorted(skill_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(skill_dir)
            s3.upload_file(str(path), bucket, f"skills/{skill_name}/{rel}")
            files.append(str(rel))
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    meta = _parse_frontmatter(skill_md)
    return {
        "skill_md": skill_md,
        "definition": {
            "name": skill_name,
            "description": meta.get("description", ""),
            "version": meta.get("version", "1.0.0"),
            "path": f"s3://{bucket}/skills/{skill_name}/",
            "files": files,
        },
    }


def ensure_default_records(workspace: WorkspaceContext) -> list[dict[str, Any]]:
    """Register the gateway targets (MCP) + sample skill bundle (AGENT_SKILLS)."""
    client = registry_control_client(workspace)
    registry_id = _registry_id(workspace)
    results: list[dict[str, Any]] = []

    gateway_url = workspace.resources.get("gateway_url", "")
    tools = mcp_client.tools_list(workspace)
    by_target: dict[str, list[dict[str, Any]]] = {}
    for tool in tools:
        if "___" in tool["name"]:
            target, short = tool["name"].split("___", 1)
            by_target.setdefault(target, []).append(
                {
                    "name": short,
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {}),
                }
            )
    for target, target_tools in sorted(by_target.items()):
        description = f"Gateway target {target} · {len(target_tools)} MCP tool(s)"
        record, created = reg.upsert_record(
            client,
            registry_id,
            name=target,
            description=description,
            descriptor_type="MCP",
            descriptors=reg.build_mcp_descriptors(
                target=target,
                description=description,
                gateway_url=gateway_url,
                tools=target_tools,
            ),
        )
        if created:
            reg.wait_record_settled(client, registry_id, record["recordId"])
            reg.submit_record(client, registry_id, record["recordId"])
        results.append({"name": target, "type": "MCP", "record_id": record["recordId"],
                        "created": created})

    bundle = upload_skill_bundle(workspace)
    record, created = reg.upsert_record(
        client,
        registry_id,
        name=SKILL_NAME,
        description=bundle["definition"]["description"][:200],
        descriptor_type="AGENT_SKILLS",
        descriptors=reg.build_skills_descriptors(
            skill_md=bundle["skill_md"], definition=bundle["definition"]
        ),
    )
    if created:
        reg.wait_record_settled(client, registry_id, record["recordId"])
        reg.submit_record(client, registry_id, record["recordId"])
    results.append({"name": SKILL_NAME, "type": "AGENT_SKILLS",
                    "record_id": record["recordId"], "created": created})
    return results


def skill_attach_path(workspace: WorkspaceContext, skill_name: str = SKILL_NAME) -> str:
    """The skills[{path}] value a harness spec uses to attach the bundle."""
    bucket = workspace.resources.get("artifacts_bucket", "")
    return f"s3://{bucket}/skills/{skill_name}/"


def console_list(
    workspace: WorkspaceContext,
    descriptor_type: str | None = None,
    status: str | None = None,
) -> list[dict]:
    return reg.list_records(
        registry_control_client(workspace), _registry_id(workspace), descriptor_type, status
    )


def console_get(workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
    return reg.get_record(
        registry_control_client(workspace), _registry_id(workspace), record_id
    )


def console_live_agent_card(
    workspace: WorkspaceContext, record_id: str, agent: Agent
) -> dict[str, Any]:
    """The card the runtime behind an A2A record serves *now* (``GetAgentCard``),
    diffed against the card the record stores.

    ``agent`` is the ledger row whose ``registry_record_id`` is ``record_id`` —
    the caller resolved and vetted it (protocol, status, workspace) so the ARN
    handed to the data plane is always the ledger's, never the browser's.
    Nothing is persisted: the record keeps the deploy-time card, AWS keeps the
    live one.
    """
    record = console_get(workspace, record_id)
    live = rt.get_agent_card(data_client(workspace), runtime_arn=agent.arn or "")
    return {
        "agent_id": agent.id,
        "runtime_arn": agent.arn,
        "status_code": live["status_code"],
        "card": live["card"],
        "diff": reg.agent_card_diff(reg.stored_a2a_card(record), live["card"]),
    }


def console_search(workspace: WorkspaceContext, query: str) -> list[dict[str, Any]]:
    return reg.search_records(
        registry_data_client(workspace), [_registry_id(workspace)], query
    )


def console_action(
    workspace: WorkspaceContext, record_id: str, action: str
) -> dict[str, Any]:
    client = registry_control_client(workspace)
    registry_id = _registry_id(workspace)
    if action == "submit":
        return reg.submit_record(client, registry_id, record_id)
    if action in ("approve", "publish"):
        return reg.approve_record(client, registry_id, record_id)
    if action == "reject":
        return reg.reject_record(client, registry_id, record_id)
    if action == "disable":
        return reg.disable_record(client, registry_id, record_id)
    raise ValueError(f"unknown action '{action}'")


def console_delete(workspace: WorkspaceContext, record_id: str) -> None:
    reg.delete_record(
        registry_control_client(workspace), _registry_id(workspace), record_id
    )


def build_gateway_record(
    *,
    gateway_name: str,
    gateway_url: str,
    target_names: list[str],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one standard MCP record for an entire AgentCore Gateway.

    Action names are copied exactly from discovery. Target identity remains
    visible through those exact names and the record description; no
    Launchpad-only fields are added to the MCP server schema.
    """
    tools: list[dict[str, Any]] = []
    for action in actions:
        name = action.get("name")
        if not isinstance(name, str) or not name:
            raise AppError(
                "governance.action_unverified",
                "every Gateway action needs an exact non-empty identifier",
                status_code=422,
            )
        tools.append(
            {
                "name": name,
                "description": str(action.get("description") or ""),
                "inputSchema": action.get("input_schema")
                or action.get("inputSchema")
                or {},
            }
        )
    description = (
        f"AgentCore Gateway {gateway_name} · "
        f"{len(target_names)} target(s) · {len(tools)} MCP tool(s)"
    )
    return {
        "name": gateway_name,
        "description": description,
        "descriptors": reg.build_mcp_descriptors(
            target=gateway_name,
            description=description,
            gateway_url=gateway_url,
            tools=tools,
        ),
    }


def _mcp_record_url(record: dict[str, Any]) -> str:
    try:
        server = json.loads(record["descriptors"]["mcp"]["server"]["inlineContent"])
        return str((server.get("remotes") or [{}])[0].get("url") or "")
    except (KeyError, TypeError, ValueError):
        return ""


def _descriptor_fingerprint(value: Any) -> Any:
    """Normalize embedded descriptor JSON before comparing AWS live state."""
    if isinstance(value, dict):
        return {key: _descriptor_fingerprint(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        return [_descriptor_fingerprint(item) for item in value]
    if isinstance(value, str):
        try:
            return _descriptor_fingerprint(json.loads(value))
        except (TypeError, ValueError):
            return value
    return value


def _registry_record_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": record.get("recordId"),
        "name": record.get("name"),
        "description": record.get("description", ""),
        "status": record.get("status"),
        "version": record.get("recordVersion"),
        "url": _mcp_record_url(record),
    }


def _list_mcp_record_details(client: Any, registry_id: str) -> list[dict[str, Any]]:
    return [
        reg.get_record(client, registry_id, summary["recordId"])
        for summary in reg.list_records(client, registry_id, "MCP")
    ]


def gateway_registry_states(
    workspace: WorkspaceContext,
    *,
    gateways: list[dict[str, Any]],
    client: Any | None = None,
    registry_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Match live Gateways to their Gateway-level and legacy Registry records."""
    client = client or registry_control_client(workspace)
    registry_id = registry_id or _registry_id(workspace)
    records = _list_mcp_record_details(client, registry_id)
    by_url: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        url = _mcp_record_url(record)
        if url:
            by_url.setdefault(url, []).append(record)

    states: dict[str, dict[str, Any]] = {}
    for gateway in gateways:
        gateway_id = gateway.get("gatewayId") or gateway.get("id")
        gateway_name = gateway.get("name")
        gateway_url = gateway.get("gatewayUrl") or gateway.get("url")
        matches = by_url.get(str(gateway_url or ""), [])
        exact = next(
            (record for record in matches if record.get("name") == gateway_name),
            None,
        )
        if exact is None:
            exact = next(
                (
                    record
                    for record in matches
                    if str(record.get("description") or "").startswith(
                        "AgentCore Gateway "
                    )
                ),
                None,
            )
        legacy = [
            record
            for record in matches
            if exact is None or record.get("recordId") != exact.get("recordId")
        ]
        if gateway_id:
            states[str(gateway_id)] = {
                "registry_record": (
                    _registry_record_summary(exact) if exact is not None else None
                ),
                "legacy_record_count": len(legacy),
            }
    return states


def gateway_registry_preview(
    workspace: WorkspaceContext,
    *,
    gateway_id: str,
    gateway_name: str,
    gateway_url: str,
    target_names: list[str],
    actions: list[dict[str, Any]],
    record_name: str | None = None,
    client: Any | None = None,
    registry_id: str | None = None,
) -> dict[str, Any]:
    """Preview the Gateway-level record and non-destructive legacy migration.

    Governance owns Gateway management checks and passes its normalized live
    Gateway/action projection here. Matching is based on the unique live
    Gateway URL; a record with the desired name but another URL is a conflict.
    """
    client = client or registry_control_client(workspace)
    registry_id = registry_id or _registry_id(workspace)
    desired_name = record_name or gateway_name
    proposed = build_gateway_record(
        gateway_name=desired_name,
        gateway_url=gateway_url,
        target_names=target_names,
        actions=actions,
    )
    records = _list_mcp_record_details(client, registry_id)
    exact = next(
        (
            record
            for record in records
            if record.get("name") == desired_name
            and _mcp_record_url(record) == gateway_url
        ),
        None,
    )
    name_conflict = next(
        (
            record
            for record in records
            if record.get("name") == desired_name
            and _mcp_record_url(record) != gateway_url
        ),
        None,
    )
    legacy = [
        record
        for record in records
        if _mcp_record_url(record) == gateway_url
        and (exact is None or record.get("recordId") != exact.get("recordId"))
    ]
    changed = bool(
        exact
        and (
            exact.get("description", "") != proposed["description"]
            or _descriptor_fingerprint(exact.get("descriptors"))
            != _descriptor_fingerprint(proposed["descriptors"])
        )
    )
    outcome = (
        "conflicted"
        if name_conflict
        else "created"
        if exact is None
        else "changed"
        if changed
        else "reused"
    )
    return {
        "gateway_id": gateway_id,
        "gateway_name": gateway_name,
        "gateway_url": gateway_url,
        "proposed": proposed,
        "outcome": outcome,
        "changed": changed,
        "exact_record": _registry_record_summary(exact) if exact else None,
        "name_conflict": _registry_record_summary(name_conflict) if name_conflict else None,
        "legacy_records": [_registry_record_summary(record) for record in legacy],
    }


def import_gateway_record(
    workspace: WorkspaceContext,
    *,
    gateway_id: str,
    gateway_name: str,
    gateway_url: str,
    target_names: list[str],
    actions: list[dict[str, Any]],
    record_name: str | None = None,
    apply_update: bool = False,
    client: Any | None = None,
    registry_id: str | None = None,
) -> dict[str, Any]:
    """Create/reuse/update and submit one Gateway-level Registry record.

    This operation never approves or deprecates a record. Governance must run
    its managed-Gateway preflight before calling it.
    """
    client = client or registry_control_client(workspace)
    registry_id = registry_id or _registry_id(workspace)
    preview = gateway_registry_preview(
        workspace,
        gateway_id=gateway_id,
        gateway_name=gateway_name,
        gateway_url=gateway_url,
        target_names=target_names,
        actions=actions,
        record_name=record_name,
        client=client,
        registry_id=registry_id,
    )
    if preview["name_conflict"]:
        raise AppError(
            "governance.registry_name_conflict",
            f"Registry name '{preview['proposed']['name']}' already points to another URL",
            detail=preview["name_conflict"],
            status_code=409,
        )
    exact = preview["exact_record"]
    if exact and exact["status"] == "DEPRECATED":
        raise AppError(
            "governance.registry_name_conflict",
            "the Gateway-level Registry record is DEPRECATED and cannot be reused",
            detail=exact,
            status_code=409,
        )

    proposed = preview["proposed"]
    update_skipped = bool(exact and preview["changed"] and not apply_update)
    if exact and (not preview["changed"] or update_skipped):
        record = reg.get_record(client, registry_id, exact["record_id"])
        created = False
    else:
        record, created = reg.upsert_record(
            client,
            registry_id,
            name=proposed["name"],
            description=proposed["description"],
            descriptor_type="MCP",
            descriptors=proposed["descriptors"],
        )
    record_id = record["recordId"]
    if created or (preview["changed"] and not update_skipped):
        record = reg.wait_record_settled(client, registry_id, record_id)
    submitted = record.get("status") == "DRAFT" and not update_skipped
    if submitted:
        reg.submit_record(client, registry_id, record_id)
        record = reg.get_record(client, registry_id, record_id)

    outcome = (
        "created"
        if created
        else "updated"
        if preview["changed"] and not update_skipped
        else "reused"
    )
    return {
        "outcome": outcome,
        "created": int(created),
        "reused": int(not created and (not preview["changed"] or update_skipped)),
        "updated": int(not created and preview["changed"] and not update_skipped),
        "skipped": int(update_skipped or (not preview["changed"] and not submitted)),
        "conflicted": 0,
        "submitted": submitted,
        "record": _registry_record_summary(record),
        "legacy_records": preview["legacy_records"],
    }


def retire_legacy_gateway_records(
    workspace: WorkspaceContext,
    *,
    gateway_record_id: str,
    legacy_record_ids: list[str],
    client: Any | None = None,
    registry_id: str | None = None,
) -> dict[str, Any]:
    """Explicitly deprecate selected legacy target records after cutover."""
    client = client or registry_control_client(workspace)
    registry_id = registry_id or _registry_id(workspace)
    gateway_record = reg.get_record(client, registry_id, gateway_record_id)
    gateway_url = _mcp_record_url(gateway_record)
    if (
        gateway_record.get("descriptorType") != "MCP"
        or gateway_record.get("status") != "APPROVED"
        or not gateway_url
    ):
        raise AppError(
            "governance.registry_record_not_approved",
            "the Gateway-level Registry record must be APPROVED before legacy retirement",
            status_code=409,
        )

    selected: list[dict[str, Any]] = []
    for record_id in dict.fromkeys(legacy_record_ids):
        record = reg.get_record(client, registry_id, record_id)
        if (
            record_id == gateway_record_id
            or record.get("descriptorType") != "MCP"
            or _mcp_record_url(record) != gateway_url
        ):
            raise AppError(
                "governance.registry_name_conflict",
                f"record {record_id} is not a legacy record for this Gateway",
                status_code=409,
            )
        selected.append(record)

    retired: list[str] = []
    skipped: list[str] = []
    for record in selected:
        record_id = record["recordId"]
        if record.get("status") == "DEPRECATED":
            skipped.append(record_id)
            continue
        reg.disable_record(client, registry_id, record_id)
        retired.append(record_id)
    return {"retired": retired, "skipped": skipped}


def _list_live_mcp_gateways(client: Any) -> list[dict[str, Any]]:
    return [
        policy_api.get_gateway(client, summary["gatewayId"])
        for summary in policy_api.list_gateways(client)
        if summary.get("protocolType") == "MCP"
    ]


def _managed_launchpad_gateway(gateway: dict[str, Any], resources: dict[str, Any]) -> bool:
    configured_id = resources.get("gateway_id")
    if configured_id:
        return gateway.get("gatewayId") == configured_id
    return bool(
        resources.get("gateway_url")
        and gateway.get("name") == "launchpad-gw"
        and gateway.get("gatewayUrl") == resources["gateway_url"]
    )


def _gateway_auth(
    gateway: dict[str, Any],
    resources: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    authorizer = gateway.get("authorizerType")
    if authorizer == "AWS_IAM":
        return "aws_iam", {"awsIam": {}}, None
    if authorizer == "NONE":
        return "none", {"none": {}}, None
    if authorizer == "CUSTOM_JWT" and _managed_launchpad_gateway(gateway, resources):
        provider_arn = resources.get("oauth_provider_arn")
        if provider_arn:
            return (
                "oauth",
                {
                    "oauth": {
                        "providerArn": provider_arn,
                        "grantType": "CLIENT_CREDENTIALS",
                        "scopes": [GATEWAY_SCOPE],
                    }
                },
                None,
            )
    return (
        "oauth" if authorizer == "CUSTOM_JWT" else None,
        None,
        "Gateway outbound auth is not mapped to a Launchpad-managed credential provider",
    )


def _gateway_attachment(gateway: dict[str, Any], resources: dict[str, Any]) -> dict[str, Any]:
    auth_type, outbound_auth, reason = _gateway_auth(gateway, resources)
    return {
        "gateway_id": gateway.get("gatewayId"),
        "gateway_arn": gateway.get("gatewayArn"),
        "gateway_name": gateway.get("name"),
        "attachable": outbound_auth is not None,
        "attachability_reason": reason,
        "auth_type": auth_type,
        "outbound_auth": outbound_auth,
    }


def _legacy_gateway_attachment(resources: dict[str, Any]) -> dict[str, Any] | None:
    if not resources.get("gateway_arn") or not resources.get("oauth_provider_arn"):
        return None
    return {
        "gateway_id": resources.get("gateway_id"),
        "gateway_arn": resources["gateway_arn"],
        "gateway_name": "launchpad-gw",
        "attachable": True,
        "attachability_reason": None,
        "auth_type": "oauth",
        "outbound_auth": {
            "oauth": {
                "providerArn": resources["oauth_provider_arn"],
                "grantType": "CLIENT_CREDENTIALS",
                "scopes": [GATEWAY_SCOPE],
            }
        },
    }


def resolve_gateway_attachments(
    tools: list[Any],
    workspace: WorkspaceContext,
    *,
    registry_client: Any | None = None,
    agentcore_client: Any | None = None,
    registry_id: str | None = None,
) -> list[dict[str, Any]]:
    """Reread selected Registry records and Gateways for Harness deployment.

    New refs carry only ``record_id`` + ``gateway_id``. Any other browser
    fields are ignored. Config-less refs retain the historical configured
    launchpad-gw fallback.
    """
    gateway_tools = [tool for tool in tools if getattr(tool, "type", None) == "gateway"]
    if not gateway_tools:
        return []
    resources = workspace.resources
    attachments: list[dict[str, Any]] = []

    if any(not getattr(tool, "config", None) for tool in gateway_tools):
        legacy = _legacy_gateway_attachment(resources)
        if legacy:
            attachments.append(legacy)

    configured = [
        getattr(tool, "config", {}) or {}
        for tool in gateway_tools
        if getattr(tool, "config", None)
    ]
    for config in configured:
        record_id = config.get("record_id")
        gateway_id = config.get("gateway_id")
        if not record_id or not gateway_id:
            raise AppError(
                "governance.gateway_unsupported",
                "Gateway tool refs must include both record_id and gateway_id",
                status_code=409,
            )
    if configured:
        registry_client = registry_client or registry_control_client(workspace)
        agentcore_client = agentcore_client or control_client(workspace)
        registry_id = registry_id or _registry_id(workspace)

    for config in configured:
        record_id = config["record_id"]
        gateway_id = config["gateway_id"]
        record = reg.get_record(registry_client, registry_id, record_id)
        if record.get("descriptorType") != "MCP" or record.get("status") != "APPROVED":
            raise AppError(
                "governance.registry_record_not_approved",
                f"Registry record {record_id} is no longer an APPROVED MCP record",
                status_code=409,
            )
        record_url = _mcp_record_url(record)
        gateway = policy_api.get_gateway(agentcore_client, gateway_id)
        if (
            gateway.get("protocolType") != "MCP"
            or not record_url
            or gateway.get("gatewayUrl") != record_url
        ):
            raise AppError(
                "governance.concurrent_change",
                "the selected Registry record no longer resolves to the selected Gateway",
                status_code=409,
            )
        attachment = _gateway_attachment(gateway, resources)
        if not attachment["attachable"]:
            raise AppError(
                "governance.gateway_unsupported",
                attachment["attachability_reason"],
                status_code=409,
            )
        attachments.append(attachment)

    unique: dict[str, dict[str, Any]] = {}
    for attachment in attachments:
        key = str(attachment["gateway_arn"])
        unique[key] = attachment
    return list(unique.values())


def attachable_records(
    workspace: WorkspaceContext,
    *,
    registry_client: Any | None = None,
    agentcore_client: Any | None = None,
    registry_id: str | None = None,
    gateways: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Catalog entries an agent can mount, sourced ONLY from APPROVED records —
    the registry lifecycle is the availability gate. MCP records whose URL
    matches a live AgentCore Gateway expose server-derived attachability;
    other MCP records remain unauthenticated remote_mcp entries."""
    registry_client = registry_client or registry_control_client(workspace)
    registry_id = registry_id or _registry_id(workspace)
    resources = workspace.resources
    gateways = (
        gateways
        if gateways is not None
        else _list_live_mcp_gateways(agentcore_client or control_client(workspace))
    )
    gateways_by_url: dict[str, list[dict[str, Any]]] = {}
    for gateway in gateways:
        if gateway.get("protocolType") == "MCP" and gateway.get("gatewayUrl"):
            gateways_by_url.setdefault(gateway["gatewayUrl"], []).append(gateway)
    mcp_servers: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    for summary in reg.list_records(registry_client, registry_id, None, "APPROVED"):
        kind = summary.get("descriptorType")
        if kind not in ("MCP", "AGENT_SKILLS"):
            continue
        record = reg.get_record(registry_client, registry_id, summary["recordId"])
        try:
            if kind == "MCP":
                server = json.loads(
                    record["descriptors"]["mcp"]["server"]["inlineContent"]
                )
                url = (server.get("remotes") or [{}])[0].get("url", "")
                if not url:
                    continue
                matches = gateways_by_url.get(url, [])
                gateway = matches[0] if len(matches) == 1 else None
                capability = (
                    _gateway_attachment(gateway, resources)
                    if gateway
                    else {
                        "gateway_id": None,
                        "gateway_arn": None,
                        "attachable": len(matches) == 0,
                        "attachability_reason": (
                            None
                            if not matches
                            else "multiple live Gateways expose the same endpoint"
                        ),
                        "auth_type": "none" if not matches else None,
                    }
                )
                mcp_servers.append(
                    {
                        "name": record["name"],
                        "description": record.get("description", ""),
                        "url": url,
                        "gateway": gateway is not None or bool(matches),
                        "record_id": record["recordId"],
                        **{
                            key: value
                            for key, value in capability.items()
                            if key != "outbound_auth"
                        },
                    }
                )
            else:
                definition = json.loads(
                    record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"]
                )
                path = definition.get("path") or ""
                if not path:
                    continue
                skills.append(
                    {
                        "name": record["name"],
                        "description": definition.get("description", ""),
                        "path": path,
                        "record_id": record["recordId"],
                    }
                )
        except (KeyError, ValueError, TypeError):
            continue  # malformed descriptor — skip, never break the catalog
    return {"mcp_servers": mcp_servers, "skills": skills}


def _require_new_name(client: Any, registry_id: str, name: str, kind: str) -> None:
    if reg.find_record(client, registry_id, name, kind):
        raise AppError(
            "registry.name_exists",
            f"a {kind} record named '{name}' already exists",
            status_code=409,
        )


def register_mcp_server(
    workspace: WorkspaceContext, name: str, description: str, url: str
) -> dict[str, Any]:
    """Register an external remote MCP server (streamable-http URL) as an MCP
    record. Starts in DRAFT — the console lifecycle (submit → approve) gates
    when it becomes attachable to agents."""
    client = registry_control_client(workspace)
    registry_id = _registry_id(workspace)
    _require_new_name(client, registry_id, name, "MCP")
    record, _ = reg.upsert_record(
        client,
        registry_id,
        name=name,
        description=description or f"remote MCP server · {url}",
        descriptor_type="MCP",
        descriptors=reg.build_mcp_descriptors(
            target=name, description=description, gateway_url=url, tools=None
        ),
    )
    return reg.wait_record_settled(client, registry_id, record["recordId"])


def _utcnow_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def register_skill_bundle(
    bundle: SkillBundle,
    workspace: WorkspaceContext,
    *,
    name_override: str | None = None,
    description_override: str | None = None,
) -> dict[str, Any]:
    """Register any acquired skill bundle: validate → reserve the name → upload
    every file under ``bundle.root`` to ``skills/{name}/{rel}`` → create the
    AGENT_SKILLS record with the real file list + provenance. Starts in DRAFT.

    The single funnel every source (inline/zip and later git/url) converges on.
    Best-effort: if record creation fails after upload, the uploaded objects are
    deleted so no orphan S3 prefix is left behind.
    """
    name = (name_override or bundle.name or "").strip()
    if not SKILL_NAME_RE.match(name):
        raise SkillValidationError(
            f"skill name '{name}' must match ^[a-z][a-z0-9-]{{2,63}}$ "
            "(set it in SKILL.md frontmatter or provide an override)"
        )
    validate_bundle(bundle)

    client = registry_control_client(workspace)
    registry_id = _registry_id(workspace)
    _require_new_name(client, registry_id, name, "AGENT_SKILLS")

    bucket = _artifacts_bucket(workspace)

    prefix = f"skills/{name}/"
    description = (description_override or bundle.description or "").strip()
    source = replace(bundle.source, imported_at=_utcnow_iso())
    definition = {
        "name": name,
        "description": description,
        "version": bundle.version,
        "path": f"s3://{bucket}/{prefix}",
        "files": bundle.files,
        "source": asdict(source),
    }
    # The serialized definition and SKILL.md share the 102,400-byte descriptor cap;
    # a large file list (many/long paths) could overflow it. Fail cleanly at the
    # Launchpad layer *before* uploading, so no S3 objects are created (AC4/AC5).
    definition_bytes = len(json.dumps(definition).encode("utf-8"))
    if definition_bytes > SKILL_MD_MAX_BYTES:
        raise SkillValidationError(
            f"skill descriptor is {definition_bytes} bytes — exceeds the "
            f"{SKILL_MD_MAX_BYTES} byte limit (too many files or paths too long)"
        )

    s3 = workspace.client("s3")
    uploaded: list[str] = []
    try:
        upload_bundle_files(bundle, bucket, prefix, s3, uploaded=uploaded)

        record, _ = reg.upsert_record(
            client,
            registry_id,
            name=name,
            description=(description or name)[:200],
            descriptor_type="AGENT_SKILLS",
            descriptors=reg.build_skills_descriptors(
                skill_md=bundle.skill_md, definition=definition
            ),
        )
        return reg.wait_record_settled(client, registry_id, record["recordId"])
    except Exception:
        _delete_keys(s3, bucket, uploaded)  # best-effort orphan cleanup
        raise


def upload_bundle_files(
    bundle: SkillBundle,
    bucket: str,
    prefix: str,
    s3: Any,
    *,
    uploaded: list[str] | None = None,
) -> list[str]:
    """Upload every file under ``bundle.root`` to ``{prefix}{rel}``. Keys are
    appended to ``uploaded`` AS they land so a mid-batch failure leaves the
    caller an exact cleanup list. Shared by the registry funnel and the
    attach-without-record path (/api/agent-skills)."""
    keys = uploaded if uploaded is not None else []
    for rel in bundle.files:
        key = f"{prefix}{rel}"
        s3.upload_file(str(bundle.root / rel), bucket, key)
        keys.append(key)
    return keys


def _delete_keys(s3: Any, bucket: str, keys: list[str]) -> None:
    for key in keys:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception:  # cleanup must never mask the original failure
            pass


def register_skill(
    workspace: WorkspaceContext, name: str, description: str, skill_md: str
) -> dict[str, Any]:
    """Register a skill from raw SKILL.md content (the console paste path). Thin
    wrapper over ``register_skill_bundle`` via the inline acquirer — behaviour is
    unchanged bar the additive ``source`` field in the descriptor."""
    bundle = bundle_from_inline(skill_md)
    try:
        return register_skill_bundle(
            bundle, workspace, name_override=name, description_override=description or None
        )
    finally:
        bundle.close()


def _bump_minor(version: str) -> str:
    """Bump the minor component while preserving a GA type suffix.

    ``1.0.0`` becomes ``1.1.0`` and ``1.0.0-skill`` becomes
    ``1.1.0-skill``. A version whose major/minor do not parse resets to a clean
    bumped baseline rather than failing the reimport.
    """
    parts = (version or "").split(".")
    try:
        major = int(parts[0])
    except (ValueError, IndexError):
        return "1.1.0"
    try:
        minor = int(parts[1])
    except (ValueError, IndexError):
        minor = 0
    patch = parts[2] if len(parts) > 2 else ""
    suffix = f"-{patch.split('-', 1)[1]}" if "-" in patch else ""
    return f"{major}.{minor + 1}.0{suffix}"


def _delete_prefix(s3: Any, bucket: str, prefix: str) -> None:
    """Delete every object under ``prefix`` (paginated list, not a single call —
    a bundle can exceed one list page). Used before a reimport re-upload so files
    removed at the source don't linger in S3."""
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    _delete_keys(s3, bucket, keys)


def reimport_skill(workspace: WorkspaceContext, record_id: str) -> dict[str, Any]:
    """Re-run the ingestion pipeline for a git/url-sourced skill record: re-acquire
    from the stored provenance, replace the whole S3 prefix, and update the record
    with a bumped ``recordVersion`` and a refreshed ``imported_at``.

    Only git/url records carry a retrievable origin — inline/zip records raise
    ``registry.not_reimportable`` (400), as do DEPRECATED records (terminal). The
    record's registered name is kept even if the source's frontmatter name
    changed, since the S3 prefix and record identity are keyed by name. Private
    git repos have no persisted token, so a private reimport surfaces the (token-
    redacted) clone/download error — accepted by design.
    """
    client = registry_control_client(workspace)
    registry_id = _registry_id(workspace)
    record = reg.get_record(client, registry_id, record_id)
    if record.get("status") == "DEPRECATED":
        raise AppError(
            "registry.not_reimportable",
            "a DEPRECATED record is terminal and cannot be reimported",
            status_code=400,
        )
    name = (record.get("name") or "").strip()
    try:
        old_definition = json.loads(
            record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"]
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise AppError(
            "registry.not_reimportable",
            "record has no readable skill descriptor to reimport",
            status_code=400,
        ) from exc
    source = old_definition.get("source") or {}
    kind = source.get("kind")
    if kind not in ("git", "url"):
        raise AppError(
            "registry.not_reimportable",
            f"only git/url-sourced skills can be reimported (source kind: {kind or 'none'})",
            status_code=400,
        )

    bundle = bundle_from_source(source)
    try:
        return _reupload_and_update(client, registry_id, record, name, bundle, workspace)
    finally:
        bundle.close()


def _reupload_and_update(
    client: Any,
    registry_id: str,
    record: dict[str, Any],
    name: str,
    bundle: SkillBundle,
    workspace: WorkspaceContext,
    *,
    description: str | None = None,
) -> dict[str, Any]:
    """Validate the re-acquired bundle, clear the old S3 prefix, upload the fresh
    files, and update the record (bumped recordVersion, refreshed provenance).

    ``description`` overrides the record's current description (the edit sub-page
    passes a new one alongside the bundle); ``None`` keeps the existing one, which
    is what reimport wants.
    """
    if not SKILL_NAME_RE.match(name):
        raise SkillValidationError(f"record name '{name}' is not a valid skill name")
    validate_bundle(bundle)

    bucket = _artifacts_bucket(workspace)

    prefix = f"skills/{name}/"
    if description is None:
        description = record.get("description") or bundle.description
    description = (description or "").strip()
    source = replace(bundle.source, imported_at=_utcnow_iso())
    new_version = _bump_minor(record.get("recordVersion") or "1.0.0")
    definition = {
        "name": name,
        "description": description,
        "version": bundle.version,
        "path": f"s3://{bucket}/{prefix}",
        "files": bundle.files,
        "source": asdict(source),
    }
    definition_bytes = len(json.dumps(definition).encode("utf-8"))
    if definition_bytes > SKILL_MD_MAX_BYTES:
        raise SkillValidationError(
            f"skill descriptor is {definition_bytes} bytes — exceeds the "
            f"{SKILL_MD_MAX_BYTES} byte limit (too many files or paths too long)"
        )

    s3 = workspace.client("s3")
    # Clear the old prefix FIRST so files dropped at the source don't linger, then
    # upload the fresh set. Unlike the create path we deliberately do NOT roll back
    # the upload if the record update later fails: the record already exists and
    # points at this prefix, so the freshly uploaded files are the correct contents
    # to leave behind (the consumer downloads the whole prefix, not the descriptor's
    # file list). Rolling them back would strand the live record over an empty
    # prefix — strictly worse. A failed update leaves only stale descriptor
    # metadata (version/files/imported_at), corrected on the next reimport.
    _delete_prefix(s3, bucket, prefix)
    for rel in bundle.files:
        key = f"{prefix}{rel}"
        s3.upload_file(str(bundle.root / rel), bucket, key)
    reg.upsert_record(
        client,
        registry_id,
        name=name,
        description=(description or name)[:200],
        descriptor_type="AGENT_SKILLS",
        descriptors=reg.build_skills_descriptors(
            skill_md=bundle.skill_md, definition=definition
        ),
        record_version=new_version,
    )
    return reg.wait_record_settled(client, registry_id, record["recordId"])


def update_record(
    record_id: str,
    workspace: WorkspaceContext,
    *,
    description: str | None = None,
    url: str | None = None,
    skill_md: str | None = None,
    bundle: SkillBundle | None = None,
) -> dict[str, Any]:
    """Edit an existing MCP/AGENT_SKILLS record from the console edit sub-page.

    Partial update: a description-only change resends the current descriptors
    WITHOUT bumping ``recordVersion``; any content change rebuilds the descriptors
    and bumps the minor version (``1.0.0``→``1.1.0``). A new ``description`` given
    alongside a content change lands in the same update call. Content branches:

    - ``url`` (MCP only): rebuild the server descriptor for the new endpoint.
    - ``skill_md`` (AGENT_SKILLS only): overwrite just ``skills/{name}/SKILL.md``,
      keeping every supporting file and the recorded provenance (only
      ``imported_at`` refreshes); the descriptor's ``files``/``source`` are kept.
    - ``bundle`` (AGENT_SKILLS only): whole-bundle replace — clear the prefix,
      upload the new files, and write a fresh descriptor (reuses the reimport
      re-upload path); the record's registered name is always preserved.

    The record's registered name is never editable (the S3 prefix and record
    identity are keyed by it). DEPRECATED records are terminal and A2A records are
    owned by agent deploys — both refuse editing (400 ``registry.not_editable``).
    """
    client = registry_control_client(workspace)
    registry_id = _registry_id(workspace)
    record = reg.get_record(client, registry_id, record_id)
    if record.get("status") == "DEPRECATED":
        raise AppError(
            "registry.not_editable",
            "a DEPRECATED record is terminal and cannot be edited",
            status_code=400,
        )
    rtype = record.get("descriptorType")
    if rtype == "A2A":
        raise AppError(
            "registry.not_editable",
            "A2A records are managed by agent deploys and cannot be edited here",
            status_code=400,
        )

    name = (record.get("name") or "").strip()
    new_desc = description if description is not None else (record.get("description") or "")

    if url is not None:
        return _update_mcp_url(client, registry_id, record, name, url, new_desc)
    if skill_md is not None:
        return _update_skill_md(
            client, registry_id, record, name, skill_md, new_desc, workspace
        )
    if bundle is not None:
        return _reupload_and_update(
            client, registry_id, record, name, bundle, workspace, description=description
        )
    # description-only: resend the current descriptors unchanged (AWS update
    # always requires descriptors) with the new description, no version bump.
    reg.upsert_record(
        client,
        registry_id,
        name=name,
        description=(new_desc or name)[:200],
        descriptor_type=rtype,
        descriptors=record.get("descriptors") or {},
    )
    return reg.wait_record_settled(client, registry_id, record["recordId"])


def _update_mcp_url(
    client: Any,
    registry_id: str,
    record: dict[str, Any],
    name: str,
    url: str,
    description: str,
) -> dict[str, Any]:
    """Rebuild an MCP record's server descriptor for a new endpoint URL and bump
    the minor version."""
    new_version = _bump_minor(record.get("recordVersion") or "1.0.0")
    reg.upsert_record(
        client,
        registry_id,
        name=name,
        description=(description or name)[:200],
        descriptor_type="MCP",
        descriptors=reg.build_mcp_descriptors(
            target=name, description=description, gateway_url=url, tools=None
        ),
        record_version=new_version,
    )
    return reg.wait_record_settled(client, registry_id, record["recordId"])


def _update_skill_md(
    client: Any,
    registry_id: str,
    record: dict[str, Any],
    name: str,
    skill_md: str,
    description: str,
    workspace: WorkspaceContext,
) -> dict[str, Any]:
    """Overwrite ONLY ``skills/{name}/SKILL.md`` and refresh the descriptor,
    leaving every supporting file in the prefix untouched (no prefix clear). The
    definition keeps its ``files``/``source`` list; ``version`` follows the new
    frontmatter (falling back to the old value) and ``source.imported_at`` is
    refreshed. Minor version bump."""
    md_bytes = len(skill_md.encode("utf-8"))
    if md_bytes > SKILL_MD_MAX_BYTES:
        raise SkillValidationError(
            f"SKILL.md is {md_bytes} bytes — exceeds the {SKILL_MD_MAX_BYTES} byte limit"
        )

    bucket = _artifacts_bucket(workspace)

    try:
        old = json.loads(
            record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"]
        )
        if not isinstance(old, dict):
            old = {}
    except (KeyError, ValueError, TypeError):
        old = {}  # malformed/absent descriptor — rebuild from scratch below

    prefix = f"skills/{name}/"
    meta = parse_frontmatter(skill_md)
    source = old.get("source")
    source = dict(source) if isinstance(source, dict) else {"kind": "inline"}
    source["imported_at"] = _utcnow_iso()
    definition = {
        "name": name,
        "description": (description or old.get("description") or name),
        "version": str(meta.get("version") or old.get("version") or "1.0.0").strip(),
        "path": f"s3://{bucket}/{prefix}",
        "files": old.get("files") or ["SKILL.md"],
        "source": source,
    }
    definition_bytes = len(json.dumps(definition).encode("utf-8"))
    if definition_bytes > SKILL_MD_MAX_BYTES:
        raise SkillValidationError(
            f"skill descriptor is {definition_bytes} bytes — exceeds the "
            f"{SKILL_MD_MAX_BYTES} byte limit (too many files or paths too long)"
        )

    s3 = workspace.client("s3")
    # Only SKILL.md changed — overwrite exactly that object; do NOT clear the
    # prefix (that would strand the supporting files the deploy-time consumer
    # downloads alongside it).
    s3.put_object(Bucket=bucket, Key=f"{prefix}SKILL.md", Body=skill_md.encode("utf-8"))

    new_version = _bump_minor(record.get("recordVersion") or "1.0.0")
    reg.upsert_record(
        client,
        registry_id,
        name=name,
        description=(definition["description"] or name)[:200],
        descriptor_type="AGENT_SKILLS",
        descriptors=reg.build_skills_descriptors(skill_md=skill_md, definition=definition),
        record_version=new_version,
    )
    return reg.wait_record_settled(client, registry_id, record["recordId"])
