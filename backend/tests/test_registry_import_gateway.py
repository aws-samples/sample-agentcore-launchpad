"""Registry → "Import Gateway targets" (WB-005): ``GET /api/registry/import-gateway/preview``
and ``POST /api/registry/import-gateway`` over the real ``LocalRegistryBackend`` with the
AgentCore control plane and the gateway's ``tools/list`` stubbed — no AWS.

Platform gateway (the workspace's ``gateway_url``) → ``ensure_default_records`` semantics
(live tools, APPROVED by ``system``, DRAFT/PENDING reconciled); any other Gateway → the
member flow (created + submitted, PENDING_APPROVAL) with ``auth_warning`` when its
authorizer has no Launchpad-managed credential.
"""

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services import mcp_client, registry_console
from app.services import registry_backend as reg
from app.services.registry_local import LocalRegistryBackend

from .conftest import set_default_resources

REGION = "cn-northwest-1"
ACCOUNT = "111122223333"
PLATFORM_URL = f"https://gw-1.gateway.bedrock-agentcore.{REGION}.amazonaws.com.cn/mcp"
OTHER_URL = f"https://gw-2.gateway.bedrock-agentcore.{REGION}.amazonaws.com.cn/mcp"


class _NotFound(Exception):
    pass


def _gateway(gateway_id: str, name: str, url: str, authorizer: str = "AWS_IAM") -> dict:
    return {
        "gatewayId": gateway_id,
        "gatewayArn": f"arn:aws-cn:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{gateway_id}",
        "gatewayUrl": url,
        "name": name,
        "status": "READY",
        "protocolType": "MCP",
        "authorizerType": authorizer,
    }


def _lambda_target(target_id: str, name: str, tools: list[str]) -> dict:
    return {
        "targetId": target_id,
        "name": name,
        "status": "READY",
        "targetConfiguration": {
            "mcp": {
                "lambda": {
                    "lambdaArn": f"arn:aws-cn:lambda:{REGION}:{ACCOUNT}:function:{name}",
                    "toolSchema": {
                        "inlinePayload": [
                            {"name": tool, "description": f"{tool} desc",
                             "inputSchema": {"type": "object"}}
                            for tool in tools
                        ]
                    },
                }
            }
        },
    }


def _dynamic_target(target_id: str, name: str) -> dict:
    """An MCP-server target without a static schema — declares no tools on the control
    plane; only the live ``tools/list`` knows them."""
    return {
        "targetId": target_id,
        "name": name,
        "status": "READY",
        "targetConfiguration": {"mcp": {"mcpServer": {"endpoint": "https://example.test/mcp"}}},
    }


def _control(gateways: list[dict], targets: dict[str, list[dict]]) -> MagicMock:
    control = MagicMock()
    control.exceptions.ResourceNotFoundException = _NotFound
    control.list_gateways.return_value = {
        "items": [{"gatewayId": g["gatewayId"], "name": g["name"], "protocolType": "MCP"}
                  for g in gateways]
    }
    by_id = {g["gatewayId"]: g for g in gateways}

    def get_gateway(gatewayIdentifier: str) -> dict:
        if gatewayIdentifier not in by_id:
            raise _NotFound(gatewayIdentifier)
        return by_id[gatewayIdentifier]

    control.get_gateway.side_effect = get_gateway
    control.list_gateway_targets.side_effect = lambda gatewayIdentifier, **_kw: {
        "items": [{"targetId": t["targetId"], "name": t["name"], "status": t["status"]}
                  for t in targets.get(gatewayIdentifier, [])]
    }
    control.get_gateway_target.side_effect = lambda gatewayIdentifier, targetId: next(
        t for t in targets[gatewayIdentifier] if t["targetId"] == targetId
    )
    control.list_tags_for_resource.return_value = {"tags": {}}
    return control


PLATFORM = _gateway("gw-1", "launchpad-gateway", PLATFORM_URL)
OTHER = _gateway("gw-2", "partner-gateway", OTHER_URL, authorizer="CUSTOM_JWT")
TARGETS = {
    "gw-1": [_dynamic_target("tgt0000001", "office-facts"),
             _lambda_target("tgt0000002", "weather", ["forecast"])],
    "gw-2": [_lambda_target("tgt0000003", "crm", ["lookup_account", "open_ticket"])],
}
LIVE_TOOLS = [
    {"name": "office-facts___lookup", "description": "look", "inputSchema": {"type": "object"}},
    {"name": "office-facts___list", "description": "list", "inputSchema": {"type": "object"}},
    {"name": "weather___forecast", "description": "live forecast", "inputSchema": {}},
]


@pytest.fixture
def env(client, monkeypatch):
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    set_default_resources({"gateway_url": PLATFORM_URL, "gateway_id": "gw-1",
                           "artifacts_bucket": "bkt"})
    control = _control([OTHER, PLATFORM], TARGETS)
    monkeypatch.setattr(registry_console, "control_client", lambda _ws: control)
    live: dict[str, Any] = {"tools": list(LIVE_TOOLS), "error": None}

    def tools_list(_ws):
        if live["error"] is not None:
            raise live["error"]
        return live["tools"]

    monkeypatch.setattr(mcp_client, "tools_list", tools_list)
    return {"client": client, "backend": backend, "control": control, "live": live}


def _mcp_records(backend) -> list[dict]:
    return [r for r in backend.list_records(_default_ws()) if r["descriptorType"] == "MCP"]


def _default_ws():
    from app.core.db import SessionLocal
    from app.models.ledger import Workspace
    from app.services.workspace import WorkspaceContext

    db = SessionLocal()
    try:
        row = db.get(Workspace, "default")
        return WorkspaceContext(account_id=ACCOUNT, region=row.region,
                                resources=dict(row.resources or {}), id="default")
    finally:
        db.close()


def _gateway_block(backend, record_id: str) -> dict:
    return backend.get_record(_default_ws(), record_id)["descriptors"]["mcp"]["gateway"]


# ---------- preview ----------


def test_preview_lists_every_gateway_platform_first_with_targets_and_tool_sources(env):
    body = env["client"].get("/api/registry/import-gateway/preview")
    assert body.status_code == 200, body.text
    gateways = body.json()["gateways"]
    assert [g["gateway_id"] for g in gateways] == ["gw-1", "gw-2"]  # platform first
    platform, other = gateways
    assert platform["platform"] is True and platform["auth"] == "aws_iam"
    assert platform["auth_warning"] is None and platform["live_error"] is None
    assert platform["gateway_arn"].startswith("arn:aws-cn:bedrock-agentcore:")
    assert [t["target"] for t in platform["targets"]] == ["office-facts", "weather"]
    office, weather = platform["targets"]
    # the dynamic target's tools come from the live tools/list; the Lambda one too (live wins)
    assert (office["tools"], office["tool_source"]) == (2, "live")
    assert (weather["tools"], weather["tool_source"]) == (1, "live")
    assert office["record_id"] is None and office["status"] is None
    assert office["imported"] is False and office["held"] is False
    assert office["target_id"] == "tgt0000001" and office["target_status"] == "READY"
    # a non-IAM gateway is listed with the warning; its tools come from the schema
    assert other["platform"] is False and other["auth"] == "oauth"
    assert other["auth_warning"]
    crm = other["targets"][0]
    assert (crm["target"], crm["tools"], crm["tool_source"]) == ("crm", 2, "schema")
    # the preview never writes
    assert _mcp_records(env["backend"]) == []


def test_preview_falls_back_to_the_schema_when_the_live_gateway_is_unreachable(env):
    env["live"]["error"] = RuntimeError("gateway mid-rollout")
    platform = env["client"].get("/api/registry/import-gateway/preview").json()["gateways"][0]
    assert platform["live_error"].startswith("RuntimeError:")
    office, weather = platform["targets"]
    assert (office["tools"], office["tool_source"]) == (0, "none")
    assert (weather["tools"], weather["tool_source"]) == (1, "schema")


# ---------- selective import, platform gateway ----------


def test_selective_import_creates_exactly_the_chosen_platform_targets_approved(env):
    res = env["client"].post("/api/registry/import-gateway", json={"target_ids": ["tgt0000001"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["gateway_id"] == "gw-1" and body["platform"] is True
    assert body["auth"] == "aws_iam" and body["live_error"] is None
    assert len(body["results"]) == 1
    item = body["results"][0]
    assert item["name"] == item["target"] == "office-facts"
    assert item["target_id"] == "tgt0000001" and item["gateway_id"] == "gw-1"
    assert item["created"] is True and item["reconciled"] is False
    assert item["status"] == "APPROVED" and item["tool_source"] == "live"
    assert "auth_warning" not in item and "error" not in item

    records = _mcp_records(env["backend"])
    assert [r["name"] for r in records] == ["office-facts"]  # weather was NOT imported
    record = env["backend"].get_record(_default_ws(), item["record_id"])
    assert record["status"] == "APPROVED"
    assert record["decidedBy"] == registry_console.PLATFORM_TARGET_ACTOR
    server = json.loads(record["descriptors"]["mcp"]["server"]["inlineContent"])
    assert server["remotes"][0]["url"] == PLATFORM_URL
    tools = json.loads(record["descriptors"]["mcp"]["tools"]["inlineContent"])["tools"]
    assert [t["name"] for t in tools] == ["office-facts___lookup", "office-facts___list"]
    gateway = record["descriptors"]["mcp"]["gateway"]
    assert gateway == {
        "gateway_id": "gw-1",
        "gateway_arn": PLATFORM["gatewayArn"],
        "auth": "aws_iam",
        "runtime_tools": ["office-facts___lookup", "office-facts___list"],
    }


def test_gateway_id_defaults_to_the_platform_gateway_and_imports_every_target(env):
    body = env["client"].post("/api/registry/import-gateway", json={}).json()
    assert body["gateway_id"] == "gw-1"
    assert [(r["name"], r["created"], r["status"]) for r in body["results"]] == [
        ("office-facts", True, "APPROVED"), ("weather", True, "APPROVED"),
    ]


def test_reimport_is_idempotent_and_the_preview_reports_imported(env):
    client = env["client"]
    first = client.post("/api/registry/import-gateway", json={"target_ids": ["tgt0000002"]})
    rid = first.json()["results"][0]["record_id"]
    second = client.post("/api/registry/import-gateway", json={"target_ids": ["tgt0000002"]})
    item = second.json()["results"][0]
    assert item["record_id"] == rid
    assert item["created"] is False and item["reconciled"] is False
    assert item["status"] == "APPROVED" and "error" not in item
    assert [r["name"] for r in _mcp_records(env["backend"])] == ["weather"]  # no duplicate

    weather = client.get("/api/registry/import-gateway/preview").json()["gateways"][0]["targets"][1]
    assert weather["record_id"] == rid and weather["status"] == "APPROVED"
    assert weather["imported"] is True and weather["held"] is False


def test_platform_record_stuck_in_draft_is_reconciled_like_sync_defaults(env):
    backend = env["backend"]
    ws = _default_ws()
    record, created = backend.upsert_record(
        ws, name="weather", description="old import", descriptor_type="MCP",
        descriptors=reg.build_mcp_descriptors(target="weather", description="old",
                                              gateway_url=PLATFORM_URL, tools=[]),
    )
    assert created and record["status"] == "DRAFT"
    item = env["client"].post("/api/registry/import-gateway",
                              json={"target_ids": ["tgt0000002"]}).json()["results"][0]
    assert item["record_id"] == record["recordId"]
    assert item["created"] is False and item["reconciled"] is True
    assert item["status"] == "APPROVED"
    after = backend.get_record(ws, record["recordId"])
    assert after["decidedBy"] == registry_console.PLATFORM_TARGET_ACTOR
    assert after["description"] == "old import"  # content is never overwritten


# ---------- name collision ----------


def test_name_held_by_a_member_registered_server_is_a_failure_item_not_a_duplicate(env):
    ws = _default_ws()
    held = registry_console.register_mcp_server(ws, "weather", "my own server",
                                                "https://mcp.example.test/mcp")
    body = env["client"].post("/api/registry/import-gateway", json={}).json()
    by_name = {r["name"]: r for r in body["results"]}
    assert by_name["office-facts"]["created"] is True
    weather = by_name["weather"]
    assert weather["error_code"] == registry_console.NAME_HELD_CODE
    assert "held by a record that is not this gateway's" in weather["error"]
    assert weather["record_id"] == held["recordId"] and weather["status"] == "DRAFT"
    assert weather["created"] is False and weather["reconciled"] is False
    records = _mcp_records(env["backend"])
    assert sorted(r["name"] for r in records) == ["office-facts", "weather"]
    assert env["backend"].get_record(ws, held["recordId"])["status"] == "DRAFT"  # untouched
    assert "gateway" not in env["backend"].get_record(ws, held["recordId"])["descriptors"]["mcp"]


# ---------- non-platform gateway ----------


def test_non_iam_gateway_imports_as_a_member_submission_with_an_auth_warning(env):
    res = env["client"].post("/api/registry/import-gateway", json={"gateway_id": "gw-2"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["gateway_id"] == "gw-2" and body["platform"] is False
    assert body["auth"] == "oauth" and body["gateway_name"] == "partner-gateway"
    item = body["results"][0]
    assert item["name"] == "crm" and item["created"] is True
    assert item["status"] == "PENDING_APPROVAL"  # submitted, an admin approves
    assert item["reconciled"] is False and item["tool_source"] == "schema"
    assert item["auth_warning"]
    record = env["backend"].get_record(_default_ws(), item["record_id"])
    assert record["status"] == "PENDING_APPROVAL" and record["decidedBy"] is None
    assert "outbound auth oauth" in record["description"]
    gateway = record["descriptors"]["mcp"]["gateway"]
    assert gateway["gateway_id"] == "gw-2" and gateway["gateway_arn"] == OTHER["gatewayArn"]
    assert gateway["auth"] == "oauth"
    assert gateway["runtime_tools"] == ["crm___lookup_account", "crm___open_ticket"]
    assert gateway["auth_note"] == item["auth_warning"]
    server = json.loads(record["descriptors"]["mcp"]["server"]["inlineContent"])
    assert server["remotes"][0]["url"] == OTHER_URL

    # re-import: already imported, still pending — a member record is never reconciled
    again = env["client"].post("/api/registry/import-gateway",
                               json={"gateway_id": "gw-2"}).json()["results"][0]
    assert again["record_id"] == item["record_id"]
    assert again["created"] is False and again["reconciled"] is False
    assert again["status"] == "PENDING_APPROVAL"


def test_a_platform_target_name_is_held_against_another_gateway(env):
    """gw-2 also exposes a target named like an imported platform target: the second
    import refuses it — one name, one record, never a silent re-point."""
    env["control"].get_gateway_target.side_effect = None
    TARGETS["gw-2"].append(_lambda_target("tgt0000004", "weather", ["forecast"]))
    try:
        env["control"].get_gateway_target.side_effect = lambda gatewayIdentifier, targetId: next(
            t for t in TARGETS[gatewayIdentifier] if t["targetId"] == targetId
        )
        env["client"].post("/api/registry/import-gateway", json={"target_ids": ["tgt0000002"]})
        body = env["client"].post("/api/registry/import-gateway",
                                  json={"gateway_id": "gw-2"}).json()
        by_name = {r["name"]: r for r in body["results"]}
        assert by_name["weather"]["error_code"] == registry_console.NAME_HELD_CODE
        assert by_name["weather"]["status"] == "APPROVED"  # the platform record's status
        assert by_name["crm"]["created"] is True
        preview = env["client"].get("/api/registry/import-gateway/preview").json()
        other = preview["gateways"][1]
        held = next(t for t in other["targets"] if t["target"] == "weather")
        assert held["held"] is True and held["imported"] is False
    finally:
        TARGETS["gw-2"].pop()


def test_preview_names_the_gateway_that_holds_a_colliding_name(env):
    """N17-obs250: two gateways of the inventory expose a target of the same name; once
    the platform one is imported, gw-2's row is `held` AND says who holds it — the
    platform gateway's record, by name (resolved from the same inventory). The refusal
    itself is unchanged; the platform's own row stays `imported` with no `held_by`."""
    TARGETS["gw-2"].append(_lambda_target("tgt0000004", "weather", ["forecast"]))
    try:
        env["client"].post("/api/registry/import-gateway", json={"target_ids": ["tgt0000002"]})
        calls_before = env["control"].list_gateways.call_count
        preview = env["client"].get("/api/registry/import-gateway/preview").json()
        assert env["control"].list_gateways.call_count == calls_before + 1  # one listing
        platform, other = preview["gateways"]
        own = next(t for t in platform["targets"] if t["target"] == "weather")
        assert own["imported"] is True and own["held"] is False and own["held_by"] is None
        held = next(t for t in other["targets"] if t["target"] == "weather")
        assert held["held"] is True
        assert held["held_by"] == {
            "record_id": own["record_id"], "name": "weather", "status": "APPROVED",
            "origin": "gateway", "gateway_id": "gw-1", "gateway_name": "launchpad-gateway",
        }
        crm = other["targets"][0]
        assert crm["held"] is False and crm["held_by"] is None
        # the import's failure item carries the same attribution
        item = next(r for r in env["client"].post(
            "/api/registry/import-gateway", json={"gateway_id": "gw-2"}
        ).json()["results"] if r["name"] == "weather")
        assert item["error_code"] == registry_console.NAME_HELD_CODE
        assert item["held_by"]["origin"] == "gateway"
        assert item["held_by"]["record_id"] == own["record_id"]
    finally:
        TARGETS["gw-2"].pop()


def test_preview_names_the_member_server_that_holds_a_name(env):
    """N17-obs250: a member-registered MCP server (external URL) under a target's name is
    attributed `origin: member`, without a gateway."""
    held_rec = registry_console.register_mcp_server(
        _default_ws(), "weather", "my own server", "https://mcp.example.test/mcp"
    )
    platform = env["client"].get("/api/registry/import-gateway/preview").json()["gateways"][0]
    weather = next(t for t in platform["targets"] if t["target"] == "weather")
    assert weather["held"] is True
    assert weather["held_by"] == {
        "record_id": held_rec["recordId"], "name": "weather", "status": "DRAFT",
        "origin": "member",
    }
    office = next(t for t in platform["targets"] if t["target"] == "office-facts")
    assert office["held_by"] is None
    item = next(r for r in env["client"].post("/api/registry/import-gateway", json={}).json()[
        "results"] if r["name"] == "weather")
    assert item["held_by"]["origin"] == "member" and "gateway_name" not in item["held_by"]
    assert _mcp_records(env["backend"]) and env["backend"].get_record(
        _default_ws(), held_rec["recordId"])["status"] == "DRAFT"  # never overwritten


# ---------- errors ----------


def test_unknown_target_id_is_a_404_before_any_write(env):
    res = env["client"].post("/api/registry/import-gateway",
                             json={"target_ids": ["tgt0000001", "tgt0000009"]})
    assert res.status_code == 404
    body = res.json()
    assert body["code"] == registry_console.TARGET_NOT_FOUND_CODE
    assert body["detail"]["target_ids"] == ["tgt0000009"]
    assert _mcp_records(env["backend"]) == []


def test_unknown_gateway_is_the_governance_404(env):
    res = env["client"].post("/api/registry/import-gateway", json={"gateway_id": "gw-9"})
    assert res.status_code == 404
    assert res.json()["code"] == "governance.gateway_not_found"


def test_without_a_registry_backend_the_routes_answer_feature_unavailable(client, monkeypatch):
    reg.set_backend(None)
    monkeypatch.setattr(registry_console, "control_client", lambda _ws: MagicMock())
    assert client.get("/api/registry/import-gateway/preview").status_code == 503
    assert client.post("/api/registry/import-gateway", json={}).status_code == 503


def test_sync_defaults_contract_is_unchanged(env, monkeypatch):
    """The whole-platform import keeps answering ``{results[{name, type, record_id,
    created, reconciled, status}]}`` — the new route is additive."""
    monkeypatch.setattr(registry_console, "upload_skill_bundle", lambda _ws: {
        "skill_md": "---\\nname: expense-report-writer\\n---\\n",
        "definition": {"name": "expense-report-writer", "description": "", "version": "1.0.0",
                       "path": "s3://bkt/skills/expense-report-writer/", "files": []},
    })
    body = env["client"].post("/api/registry/sync-defaults").json()
    mcp = [r for r in body["results"] if r["type"] == "MCP"]
    assert [(r["name"], r["created"], r["status"]) for r in mcp] == [
        ("office-facts", True, "APPROVED"), ("weather", True, "APPROVED"),
    ]
    assert set(mcp[0]) == {"name", "type", "record_id", "created", "reconciled", "status"}
