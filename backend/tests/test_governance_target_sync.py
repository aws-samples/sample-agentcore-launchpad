"""Gateway target synchronization (SynchronizeGatewayTargets) with a stub control client.

The sync is a synchronous control-plane call journaled inline in ``policy_changes``.
The managed-gateway and synchronizable-target gates run before any AWS mutation
and answer 409 with a stable ``detail.reason``.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.models.ledger import PolicyChange
from app.routers import governance as governance_router
from app.services import governance
from app.services.agentcore import policy

from .conftest import ws_ctx

GW = "gw-1"
TARGET = "ABCDEFGH12"
SYNC_AT = datetime(2026, 9, 5, 8, 0, tzinfo=UTC)


def _mcp_server(*, static: bool = False, listing: str = "ALL") -> dict:
    server = {"endpoint": "https://mcp.example.test/mcp", "listingMode": listing}
    if static:
        server["mcpToolSchema"] = {
            "inlinePayload": [{"name": "t", "description": "d", "inputSchema": {"type": "object"}}]
        }
    return {"mcp": {"mcpServer": server}}


def _target(target_id: str = TARGET, *, status: str = "READY", config: dict | None = None, **over):
    value = {
        "targetId": target_id,
        "gatewayArn": f"arn:aws:bedrock-agentcore:us-west-2:123:gateway/{GW}",
        "name": f"target-{target_id}",
        "description": "dynamic mcp",
        "status": status,
        "statusReasons": [],
        "targetConfiguration": config if config is not None else _mcp_server(),
        "createdAt": SYNC_AT,
        "updatedAt": SYNC_AT,
        "lastSynchronizedAt": SYNC_AT,
    }
    value.update(over)
    return value


def _control(*, managed: bool = True, target: dict | None = None) -> MagicMock:
    current = target if target is not None else _target()
    control = MagicMock()
    control.get_gateway.return_value = {
        "gatewayId": GW,
        "gatewayArn": f"arn:aws:bedrock-agentcore:us-west-2:123:gateway/{GW}",
        "gatewayUrl": f"https://{GW}.example.test/mcp",
        "name": "alpha",
        "status": "READY",
        "statusReasons": [],
        "protocolType": "MCP",
        "authorizerType": "AWS_IAM",
        "roleArn": "arn:aws:iam::123:role/gateway",
        "updatedAt": datetime.now(UTC),
    }
    control.list_tags_for_resource.return_value = {
        "tags": dict(policy.MANAGED_TAGS) if managed else {}
    }
    control.list_gateways.return_value = {"items": [{"gatewayId": GW, "protocolType": "MCP"}]}
    control.list_gateway_targets.return_value = {"items": [{"targetId": current["targetId"]}]}
    control.get_gateway_target.return_value = current
    control.synchronize_gateway_targets.return_value = {
        "targets": [
            {**current, "status": "SYNCHRONIZING", "lastSynchronizedAt": SYNC_AT},
        ]
    }
    return control


@pytest.fixture
def install(monkeypatch):
    def _install(control: MagicMock) -> MagicMock:
        monkeypatch.setattr(governance_router, "control_client", lambda _ws=None: control)
        return control

    return _install


def _journal() -> list[PolicyChange]:
    db = SessionLocal()
    try:
        return db.query(PolicyChange).order_by(PolicyChange.created_at).all()
    finally:
        db.close()


def _post(client, target_id: str = TARGET):
    return client.post(f"/api/governance/gateways/{GW}/targets/{target_id}/synchronize")


# ---- happy path -------------------------------------------------------------


def test_sync_calls_aws_once_and_journals_the_target(client, install):
    control = install(_control())

    response = _post(client)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["id"] == TARGET
    assert body["name"] == f"target-{TARGET}"
    assert body["status"] == "SYNCHRONIZING"
    assert body["last_synchronized_at"].startswith("2026-09-05T08:00")
    assert body["listing_mode"] == "ALL"
    assert body["synchronizable"] is False
    assert body["not_synchronizable_reason"] == "synchronizing"
    assert set(body) == {
        "id",
        "name",
        "status",
        "status_reasons",
        "description",
        "listing_mode",
        "last_synchronized_at",
        "synchronizable",
        "not_synchronizable_reason",
    }
    control.synchronize_gateway_targets.assert_called_once_with(
        gatewayIdentifier=GW, targetIdList=[TARGET]
    )

    rows = _journal()
    assert len(rows) == 1
    row = rows[0]
    assert row.operation == "target.synchronize"
    assert row.status == "succeeded"
    assert row.gateway_id == GW
    assert row.before["id"] == TARGET
    assert row.before["status"] == "READY"
    assert row.requested == {"target_id": TARGET, "target_name": f"target-{TARGET}"}
    assert row.after["targetId"] == TARGET
    assert row.after["status"] == "SYNCHRONIZING"
    assert row.completed_at is not None


# ---- gates before any AWS call ----------------------------------------------


def test_unmanaged_gateway_is_refused_before_calling_aws(client, install):
    control = install(_control(managed=False))

    response = _post(client)

    assert response.status_code == 409
    assert response.json()["code"] == "governance.gateway_not_managed"
    control.synchronize_gateway_targets.assert_not_called()
    control.get_gateway_target.assert_not_called()
    assert _journal() == []


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (
            {"mcp": {"lambda": {"lambdaArn": "arn:aws:lambda:x", "toolSchema": {}}}},
            "not_mcp_server",
        ),
        ({"mcp": {"openApiSchema": {"inlinePayload": "{}"}}}, "not_mcp_server"),
        (_mcp_server(static=True), "static_tool_schema"),
    ],
)
def test_static_targets_are_not_synchronizable(client, install, config, reason):
    control = install(_control(target=_target(config=config)))

    response = _post(client)

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "governance.target_not_synchronizable"
    assert body["detail"]["reason"] == reason
    assert body["detail"]["target_id"] == TARGET
    control.synchronize_gateway_targets.assert_not_called()
    assert _journal() == []


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("CREATE_PENDING_AUTH", "pending_auth"),
        ("UPDATE_PENDING_AUTH", "pending_auth"),
        ("SYNCHRONIZE_PENDING_AUTH", "pending_auth"),
        ("SYNCHRONIZING", "synchronizing"),
        ("CREATING", "not_ready"),
        ("UPDATING", "not_ready"),
        ("DELETING", "not_ready"),
    ],
)
def test_transient_and_pending_auth_targets_are_refused(client, install, status, reason):
    control = install(_control(target=_target(status=status)))

    response = _post(client)

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "governance.target_not_synchronizable"
    assert body["detail"]["reason"] == reason
    assert body["detail"]["status"] == status
    control.synchronize_gateway_targets.assert_not_called()
    assert _journal() == []


@pytest.mark.parametrize(
    "status", ["READY", "SYNCHRONIZE_UNSUCCESSFUL", "UPDATE_UNSUCCESSFUL", "FAILED"]
)
def test_settled_statuses_are_synchronizable(client, install, status):
    control = install(_control(target=_target(status=status)))

    response = _post(client)

    assert response.status_code == 202
    control.synchronize_gateway_targets.assert_called_once()


def test_target_id_must_be_ten_alphanumerics(client, install):
    control = install(_control())

    response = _post(client, "not-an-id")

    assert response.status_code == 422
    control.synchronize_gateway_targets.assert_not_called()


# ---- AWS failure ------------------------------------------------------------


def test_aws_conflict_maps_to_409_and_leaves_a_failed_journal_row(client, install):
    control = install(_control())
    control.synchronize_gateway_targets.side_effect = ClientError(
        {"Error": {"Code": "ConflictException", "Message": "target is busy"}},
        "SynchronizeGatewayTargets",
    )

    response = _post(client)

    assert response.status_code == 409
    assert response.json()["code"] == "aws.conflict"
    rows = _journal()
    assert len(rows) == 1
    assert rows[0].operation == "target.synchronize"
    assert rows[0].status == "failed"
    assert "target is busy" in (rows[0].error or "")
    assert rows[0].after is None


def test_missing_target_maps_to_404_without_a_journal_row(client, install):
    control = install(_control())
    control.get_gateway_target.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no such target"}},
        "GetGatewayTarget",
    )

    response = _post(client)

    assert response.status_code == 404
    control.synchronize_gateway_targets.assert_not_called()
    assert _journal() == []


# ---- gateway_detail projection ----------------------------------------------


def test_gateway_detail_targets_expose_sync_projection():
    dynamic = _target("DYNAMIC001")
    static = _target("STATIC0001", config=_mcp_server(static=True, listing="ALL"))
    lambda_target = _target(
        "LAMBDA0001",
        config={"mcp": {"lambda": {"lambdaArn": "arn:aws:lambda:x", "toolSchema": {}}}},
        lastSynchronizedAt=None,
    )
    pending = _target("PENDING001", status="SYNCHRONIZE_PENDING_AUTH")
    by_id = {t["targetId"]: t for t in (dynamic, static, lambda_target, pending)}
    control = _control()
    control.list_gateway_targets.return_value = {
        "items": [{"targetId": target_id} for target_id in by_id]
    }
    control.get_gateway_target.side_effect = lambda gatewayIdentifier, targetId: by_id[targetId]
    control.list_registry_records.return_value = {"registryRecords": []}
    governance.invalidate_gateway_cache()

    detail = governance.gateway_detail(control, MagicMock(), GW, ws_ctx({"registry_id": ""}))

    targets = {t["id"]: t for t in detail["targets"]}
    assert targets["DYNAMIC001"]["synchronizable"] is True
    assert targets["DYNAMIC001"]["not_synchronizable_reason"] is None
    assert targets["DYNAMIC001"]["last_synchronized_at"].startswith("2026-09-05T08:00")
    assert targets["DYNAMIC001"]["listing_mode"] == "ALL"
    assert targets["STATIC0001"]["synchronizable"] is False
    assert targets["STATIC0001"]["not_synchronizable_reason"] == "static_tool_schema"
    assert targets["LAMBDA0001"]["synchronizable"] is False
    assert targets["LAMBDA0001"]["not_synchronizable_reason"] == "not_mcp_server"
    assert targets["LAMBDA0001"]["last_synchronized_at"] is None
    assert targets["LAMBDA0001"]["listing_mode"] is None
    assert targets["PENDING001"]["synchronizable"] is False
    assert targets["PENDING001"]["not_synchronizable_reason"] == "pending_auth"
    # the static MCP-server schema still yields discovered actions; dynamic ones do not
    assert {a["target_id"] for a in detail["actions"]} <= {"STATIC0001", "LAMBDA0001"}
