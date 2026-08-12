"""Gateway-level Registry records, attachability, and deploy-time resolution."""

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.core.errors import AppError
from app.schemas.agent import ToolRef
from app.schemas.governance import RegistryImportRequest
from app.services import governance
from app.services import registry_console as console
from app.services.agentcore import policy
from app.services.agentcore import registry as reg

from .conftest import ws_ctx


def _mcp_record(
    record_id: str,
    name: str,
    url: str,
    *,
    status: str = "APPROVED",
    description: str = "",
) -> dict:
    return {
        "recordId": record_id,
        "name": name,
        "description": description,
        "descriptorType": "MCP",
        "status": status,
        "recordVersion": "1.0.0",
        "descriptors": reg.build_mcp_descriptors(
            target=name,
            description=description,
            gateway_url=url,
            tools=[],
        ),
    }


def test_build_gateway_record_aggregates_exact_actions():
    record = console.build_gateway_record(
        gateway_name="finance-gw",
        gateway_url="https://finance.example/mcp",
        target_names=["ledger", "forecast"],
        actions=[
            {
                "name": "ledger___get_entry",
                "description": "Get one entry",
                "input_schema": {"type": "object"},
            },
            {
                "name": "forecast___run",
                "description": "Run forecast",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ],
    )
    server = json.loads(record["descriptors"]["mcp"]["server"]["inlineContent"])
    tools = json.loads(record["descriptors"]["mcp"]["tools"]["inlineContent"])["tools"]
    assert server["name"] == "io.launchpad/finance-gw"
    assert server["remotes"] == [
        {"type": "streamable-http", "url": "https://finance.example/mcp"}
    ]
    assert [tool["name"] for tool in tools] == [
        "ledger___get_entry",
        "forecast___run",
    ]
    assert "2 target(s)" in record["description"]


def test_gateway_preview_detects_legacy_and_name_conflict(monkeypatch):
    gateway_url = "https://gw.example/mcp"
    records = {
        "legacy": _mcp_record("legacy", "hr-database", gateway_url),
        "conflict": _mcp_record("conflict", "launchpad-gw", "https://other.example/mcp"),
    }
    monkeypatch.setattr(
        console.reg,
        "list_records",
        lambda *_args: [
            {"recordId": "legacy"},
            {"recordId": "conflict"},
        ],
    )
    monkeypatch.setattr(
        console.reg,
        "get_record",
        lambda _client, _registry_id, record_id: records[record_id],
    )

    preview = console.gateway_registry_preview(
        ws_ctx(),
        gateway_id="gw-1",
        gateway_name="launchpad-gw",
        gateway_url=gateway_url,
        target_names=["hr-database"],
        actions=[],
        client=object(),
        registry_id="registry",
    )
    assert preview["outcome"] == "conflicted"
    assert preview["name_conflict"]["record_id"] == "conflict"
    assert [record["record_id"] for record in preview["legacy_records"]] == ["legacy"]


def test_gateway_registry_states_match_gateway_record_and_legacy(monkeypatch):
    gateway_url = "https://gw.example/mcp"
    records = {
        "gateway": _mcp_record(
            "gateway",
            "custom-catalog-name",
            gateway_url,
            description="AgentCore Gateway custom-catalog-name · 2 target(s)",
        ),
        "legacy": _mcp_record("legacy", "hr-database", gateway_url),
    }
    monkeypatch.setattr(
        console.reg,
        "list_records",
        lambda *_args: [{"recordId": record_id} for record_id in records],
    )
    monkeypatch.setattr(
        console.reg,
        "get_record",
        lambda _client, _registry_id, record_id: records[record_id],
    )

    states = console.gateway_registry_states(
        ws_ctx(),
        gateways=[
            {
                "gatewayId": "gw-1",
                "name": "launchpad-gw",
                "gatewayUrl": gateway_url,
            }
        ],
        client=object(),
        registry_id="registry",
    )

    assert states["gw-1"]["registry_record"]["record_id"] == "gateway"
    assert states["gw-1"]["legacy_record_count"] == 1


def test_governance_registry_import_requires_managed_and_fresh_gateway(monkeypatch):
    updated_at = datetime.now(UTC)
    gateway = {
        "gatewayId": "gw-1",
        "gatewayArn": "arn:gateway:gw-1",
        "gatewayUrl": "https://gw.example/mcp",
        "name": "launchpad-gw",
        "status": "READY",
        "updatedAt": updated_at,
        "protocolType": "MCP",
    }
    control = MagicMock()
    control.get_gateway.return_value = gateway
    control.list_gateway_targets.return_value = {"items": []}
    control.list_tags_for_resource.return_value = {"tags": {}}
    request = RegistryImportRequest(expected_gateway_updated_at=updated_at)

    with pytest.raises(AppError) as unmanaged:
        governance.import_gateway_registry(control, "gw-1", request, ws_ctx())
    assert unmanaged.value.code == "governance.gateway_not_managed"

    control.list_tags_for_resource.return_value = {"tags": policy.MANAGED_TAGS}
    stale_request = RegistryImportRequest(
        expected_gateway_updated_at=updated_at - timedelta(seconds=1)
    )
    with pytest.raises(AppError) as stale:
        governance.import_gateway_registry(control, "gw-1", stale_request, ws_ctx())
    assert stale.value.code == "governance.concurrent_change"

    monkeypatch.setattr(
        governance.registry_console,
        "import_gateway_record",
        lambda _ws, **kwargs: {"outcome": "created", "gateway_id": kwargs["gateway_id"]},
    )
    result = governance.import_gateway_registry(control, "gw-1", request, ws_ctx())
    assert result == {"outcome": "created", "gateway_id": "gw-1"}


def test_gateway_import_reuses_without_update_and_submits_draft(monkeypatch):
    exact = _mcp_record(
        "gateway-record",
        "launchpad-gw",
        "https://gw.example/mcp",
        status="DRAFT",
    )
    preview = {
        "name_conflict": None,
        "exact_record": console._registry_record_summary(exact),
        "changed": False,
        "proposed": {
            "name": "launchpad-gw",
            "description": exact["description"],
            "descriptors": exact["descriptors"],
        },
        "legacy_records": [],
    }
    monkeypatch.setattr(
        console, "gateway_registry_preview", lambda _ws, **_kwargs: preview
    )
    monkeypatch.setattr(
        console.reg,
        "upsert_record",
        lambda *_args, **_kwargs: pytest.fail("unchanged import must not update"),
    )
    statuses = iter([exact, {**exact, "status": "PENDING_APPROVAL"}])
    monkeypatch.setattr(console.reg, "get_record", lambda *_args: next(statuses))
    submitted: list[str] = []
    monkeypatch.setattr(
        console.reg,
        "submit_record",
        lambda _client, _registry_id, record_id: submitted.append(record_id),
    )

    result = console.import_gateway_record(
        ws_ctx(),
        gateway_id="gw-1",
        gateway_name="launchpad-gw",
        gateway_url="https://gw.example/mcp",
        target_names=[],
        actions=[],
        client=object(),
        registry_id="registry",
    )
    assert result["outcome"] == "reused"
    assert result["submitted"] is True
    assert result["record"]["status"] == "PENDING_APPROVAL"
    assert submitted == ["gateway-record"]


def test_gateway_import_requires_apply_update_for_changed_record(monkeypatch):
    exact = _mcp_record(
        "gateway-record",
        "launchpad-gw",
        "https://gw.example/mcp",
        status="DRAFT",
    )
    preview = {
        "name_conflict": None,
        "exact_record": console._registry_record_summary(exact),
        "changed": True,
        "proposed": {
            "name": "launchpad-gw",
            "description": "new metadata",
            "descriptors": exact["descriptors"],
        },
        "legacy_records": [],
    }
    monkeypatch.setattr(
        console, "gateway_registry_preview", lambda _ws, **_kwargs: preview
    )
    monkeypatch.setattr(
        console.reg,
        "upsert_record",
        lambda *_args, **_kwargs: pytest.fail("update needs explicit apply_update"),
    )
    monkeypatch.setattr(console.reg, "get_record", lambda *_args: exact)
    monkeypatch.setattr(
        console.reg,
        "submit_record",
        lambda *_args: pytest.fail("stale DRAFT record must not be submitted"),
    )

    result = console.import_gateway_record(
        ws_ctx(),
        gateway_id="gw-1",
        gateway_name="launchpad-gw",
        gateway_url="https://gw.example/mcp",
        target_names=[],
        actions=[],
        client=object(),
        registry_id="registry",
    )
    assert result["outcome"] == "reused"
    assert result["submitted"] is False
    assert result["skipped"] == 1


def test_gateway_import_updates_after_explicit_confirmation(monkeypatch):
    exact = _mcp_record(
        "gateway-record",
        "launchpad-gw",
        "https://gw.example/mcp",
    )
    preview = {
        "name_conflict": None,
        "exact_record": console._registry_record_summary(exact),
        "changed": True,
        "proposed": {
            "name": "launchpad-gw",
            "description": "new metadata",
            "descriptors": exact["descriptors"],
        },
        "legacy_records": [],
    }
    monkeypatch.setattr(
        console, "gateway_registry_preview", lambda _ws, **_kwargs: preview
    )
    updated = {**exact, "description": "new metadata", "status": "APPROVED"}
    monkeypatch.setattr(
        console.reg,
        "upsert_record",
        lambda *_args, **_kwargs: ({"recordId": "gateway-record"}, False),
    )
    monkeypatch.setattr(console.reg, "wait_record_settled", lambda *_args: updated)

    result = console.import_gateway_record(
        ws_ctx(),
        gateway_id="gw-1",
        gateway_name="launchpad-gw",
        gateway_url="https://gw.example/mcp",
        target_names=[],
        actions=[],
        apply_update=True,
        client=object(),
        registry_id="registry",
    )
    assert result["outcome"] == "updated"
    assert result["updated"] == 1
    assert result["skipped"] == 0


def test_legacy_retirement_requires_approved_gateway_record(monkeypatch):
    gateway = _mcp_record(
        "gateway-record",
        "launchpad-gw",
        "https://gw.example/mcp",
        status="PENDING_APPROVAL",
    )
    monkeypatch.setattr(console.reg, "get_record", lambda *_args: gateway)
    with pytest.raises(AppError) as error:
        console.retire_legacy_gateway_records(
            ws_ctx(),
            gateway_record_id="gateway-record",
            legacy_record_ids=["legacy"],
            client=object(),
            registry_id="registry",
        )
    assert error.value.code == "governance.registry_record_not_approved"


def test_legacy_retirement_only_deprecates_selected_matching_records(monkeypatch):
    url = "https://gw.example/mcp"
    records = {
        "gateway": _mcp_record("gateway", "launchpad-gw", url),
        "legacy-a": _mcp_record("legacy-a", "hr-database", url),
        "legacy-b": _mcp_record("legacy-b", "office-facts", url),
    }
    monkeypatch.setattr(
        console.reg,
        "get_record",
        lambda _client, _registry_id, record_id: records[record_id],
    )
    disabled: list[str] = []
    monkeypatch.setattr(
        console.reg,
        "disable_record",
        lambda _client, _registry_id, record_id: disabled.append(record_id),
    )
    result = console.retire_legacy_gateway_records(
            ws_ctx(),
        gateway_record_id="gateway",
        legacy_record_ids=["legacy-b"],
        client=object(),
        registry_id="registry",
    )
    assert result == {"retired": ["legacy-b"], "skipped": []}
    assert disabled == ["legacy-b"]


def test_attachables_derive_gateway_auth_server_side(monkeypatch):
    resources = {
        "gateway_id": "managed",
        "oauth_provider_arn": "arn:provider:managed",
    }
    workspace = ws_ctx(resources)
    urls = {
        "iam": "https://iam.example/mcp",
        "none": "https://none.example/mcp",
        "managed": "https://managed.example/mcp",
        "external": "https://external.example/mcp",
        "remote": "https://remote.example/mcp",
    }
    records = {
        key: _mcp_record(key, key, url)
        for key, url in urls.items()
    }
    monkeypatch.setattr(
        console.reg,
        "list_records",
        lambda *_args: [
            {"recordId": key, "descriptorType": "MCP"}
            for key in records
        ],
    )
    monkeypatch.setattr(
        console.reg,
        "get_record",
        lambda _client, _registry_id, record_id: records[record_id],
    )
    gateways = [
        {
            "gatewayId": gateway_id,
            "gatewayArn": f"arn:gateway:{gateway_id}",
            "gatewayUrl": urls[gateway_id],
            "name": "launchpad-gw" if gateway_id == "managed" else f"{gateway_id}-gw",
            "protocolType": "MCP",
            "authorizerType": authorizer,
        }
        for gateway_id, authorizer in [
            ("iam", "AWS_IAM"),
            ("none", "NONE"),
            ("managed", "CUSTOM_JWT"),
            ("external", "CUSTOM_JWT"),
        ]
    ]

    result = console.attachable_records(
        workspace,
        registry_client=object(),
        registry_id="registry",
        gateways=gateways,
    )
    by_name = {item["name"]: item for item in result["mcp_servers"]}
    assert (by_name["iam"]["attachable"], by_name["iam"]["auth_type"]) == (True, "aws_iam")
    assert (by_name["none"]["attachable"], by_name["none"]["auth_type"]) == (True, "none")
    assert (by_name["managed"]["attachable"], by_name["managed"]["auth_type"]) == (
        True,
        "oauth",
    )
    assert by_name["external"]["attachable"] is False
    assert by_name["external"]["auth_type"] == "oauth"
    assert by_name["remote"]["gateway"] is False
    assert by_name["remote"]["attachable"] is True


def test_resolve_gateway_attachments_ignores_browser_auth_and_deduplicates(monkeypatch):
    url = "https://iam.example/mcp"
    record = _mcp_record("record", "finance-gw", url)
    monkeypatch.setattr(console.reg, "get_record", lambda *_args: record)
    monkeypatch.setattr(
        console.policy_api,
        "get_gateway",
        lambda _client, _gateway_id: {
            "gatewayId": "gw-iam",
            "gatewayArn": "arn:gateway:real",
            "gatewayUrl": url,
            "name": "finance-gw",
            "protocolType": "MCP",
            "authorizerType": "AWS_IAM",
        },
    )
    workspace = ws_ctx({"oauth_provider_arn": "arn:provider:real"})
    tools = [
        ToolRef(
            type="gateway",
            name="finance",
            config={
                "record_id": "record",
                "gateway_id": "gw-iam",
                "providerArn": "arn:provider:attacker",
                "outboundAuth": {"oauth": {"providerArn": "arn:provider:attacker"}},
            },
        ),
        ToolRef(
            type="gateway",
            name="finance-copy",
            config={"record_id": "record", "gateway_id": "gw-iam"},
        ),
    ]
    attachments = console.resolve_gateway_attachments(
        tools,
        workspace,
        registry_client=MagicMock(),
        agentcore_client=MagicMock(),
        registry_id="registry",
    )
    assert attachments == [
        {
            "gateway_id": "gw-iam",
            "gateway_arn": "arn:gateway:real",
            "gateway_name": "finance-gw",
            "attachable": True,
            "attachability_reason": None,
            "auth_type": "aws_iam",
            "outbound_auth": {"awsIam": {}},
        }
    ]


def test_resolve_configless_gateway_ref_keeps_legacy_fallback(monkeypatch):
    attachments = console.resolve_gateway_attachments(
        [ToolRef(type="gateway", name="hr-database")],
        ws_ctx({
            "gateway_id": "launchpad-id",
            "gateway_arn": "arn:gateway:launchpad",
            "oauth_provider_arn": "arn:provider:launchpad",
        }),
    )
    assert attachments[0]["gateway_arn"] == "arn:gateway:launchpad"
    assert attachments[0]["outbound_auth"]["oauth"]["providerArn"] == (
        "arn:provider:launchpad"
    )
