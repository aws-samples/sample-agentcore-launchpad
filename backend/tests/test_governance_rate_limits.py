"""Gateway rate limits: list/create/update/delete with a stub control client.

Every mutation is a synchronous control-plane call journaled inline in
``policy_changes``; validation runs before any AWS call and answers 422 with a
stable ``detail.reason``.
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.models.ledger import PolicyChange
from app.routers import governance as governance_router
from app.services.agentcore import policy

GW = "gw-1"
BASE = f"/api/governance/gateways/{GW}/rate-limits"


class _NotFound(Exception):
    """Stands in for the preview SDK's ResourceNotFoundException."""


def _detail(rate_limit_id: str = "rl-1", **overrides) -> dict:
    value = {
        "rateLimitId": rate_limit_id,
        "gatewayIdentifier": GW,
        "description": "per target",
        "dimensionKeys": ["targetName"],
        "entries": [
            {"dimensions": {"targetName": "*"}, "requests": [{"rate": 60.0, "period": "minute"}]}
        ],
        "status": "ACTIVE",
        "createdAt": datetime(2026, 9, 5, tzinfo=UTC),
        "updatedAt": datetime(2026, 9, 5, tzinfo=UTC),
    }
    value.update(overrides)
    return value


def _control(*, managed: bool = True) -> MagicMock:
    control = MagicMock()
    control.exceptions.ResourceNotFoundException = _NotFound
    control.get_gateway.return_value = {
        "gatewayId": GW,
        "gatewayArn": f"arn:aws:bedrock-agentcore:us-west-2:123:gateway/{GW}",
        "name": "alpha",
        "status": "READY",
        "protocolType": "MCP",
        "authorizerType": "AWS_IAM",
        "roleArn": "arn:aws:iam::123:role/gateway",
        "updatedAt": datetime.now(UTC),
    }
    control.list_tags_for_resource.return_value = {
        "tags": dict(policy.MANAGED_TAGS) if managed else {}
    }
    control.list_gateway_rate_limits.return_value = {"rateLimits": [_detail()]}
    control.get_gateway_rate_limit.return_value = _detail()
    control.create_gateway_rate_limit.return_value = _detail(status="CREATING")
    control.update_gateway_rate_limit.return_value = _detail(status="UPDATING")
    control.delete_gateway_rate_limit.return_value = {"rateLimitId": "rl-1", "status": "DELETING"}
    return control


@pytest.fixture
def control(monkeypatch) -> MagicMock:
    stub = _control()
    monkeypatch.setattr(governance_router, "control_client", lambda _ws=None: stub)
    return stub


def _entry(value: str = "*", **metrics) -> dict:
    return {"dimensions": {"targetName": value}, **metrics}


def _create_body(**overrides) -> dict:
    body = {
        "dimension_keys": ["targetName"],
        "entries": [_entry(requests=[{"rate": 60, "period": "minute"}])],
        "description": "per target",
    }
    body.update(overrides)
    return body


def _journal() -> list[PolicyChange]:
    db = SessionLocal()
    try:
        return db.query(PolicyChange).order_by(PolicyChange.created_at).all()
    finally:
        db.close()


# ---- list -------------------------------------------------------------------


def test_list_rate_limits_follows_every_next_token_page(client, control):
    control.list_gateway_rate_limits.side_effect = [
        {"rateLimits": [_detail("rl-1")], "nextToken": "page-2"},
        {"rateLimits": [_detail("rl-2")]},
    ]
    control.list_tags_for_resource.return_value = {"tags": {}}  # read works unmanaged

    response = client.get(BASE)

    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body["rate_limits"]] == ["rl-1", "rl-2"]
    first = body["rate_limits"][0]
    assert first["dimension_keys"] == ["targetName"]
    assert first["entries"][0]["requests"] == [{"rate": 60.0, "period": "minute"}]
    assert first["status"] == "ACTIVE"
    assert first["updated_at"].startswith("2026-09-05")
    calls = control.list_gateway_rate_limits.call_args_list
    assert calls[0].kwargs == {"gatewayIdentifier": GW, "maxResults": 100}
    assert calls[1].kwargs["nextToken"] == "page-2"
    assert _journal() == []  # reads are not journaled


# ---- create -----------------------------------------------------------------


def test_create_sends_exactly_the_validated_payload_and_journals_it(client, control):
    response = client.post(
        BASE,
        json=_create_body(
            dimension_keys=["targetName", "$.context.jwt.sub"],
            entries=[
                {
                    "dimensions": {"targetName": "office-facts", "$.context.jwt.sub": "*"},
                    "requests": [{"rate": 10, "period": "second"}],
                    "tokens": [{"rate": 5000, "period": "minute"}],
                },
                {
                    "dimensions": {"targetName": "*", "$.context.jwt.sub": "*"},
                    "connections": [{"rate": 2, "period": "second"}],
                },
            ],
        ),
    )

    assert response.status_code == 201, response.text
    assert response.json()["id"] == "rl-1"
    assert response.json()["status"] == "CREATING"
    kwargs = control.create_gateway_rate_limit.call_args.kwargs
    assert set(kwargs) == {
        "gatewayIdentifier",
        "dimensionKeys",
        "entries",
        "description",
        "clientToken",
    }
    assert kwargs["gatewayIdentifier"] == GW
    assert kwargs["dimensionKeys"] == ["targetName", "$.context.jwt.sub"]
    assert kwargs["description"] == "per target"
    assert len(kwargs["clientToken"]) >= 33
    assert kwargs["entries"] == [
        {
            "dimensions": {"targetName": "office-facts", "$.context.jwt.sub": "*"},
            "requests": [{"rate": 10.0, "period": "second"}],
            "tokens": [{"rate": 5000.0, "period": "minute"}],
        },
        {
            "dimensions": {"targetName": "*", "$.context.jwt.sub": "*"},
            "connections": [{"rate": 2.0, "period": "second"}],
        },
    ]

    rows = _journal()
    assert len(rows) == 1
    row = rows[0]
    assert row.operation == "rate_limit.create"
    assert row.status == "succeeded"
    assert row.gateway_id == GW and row.gateway_name == "alpha"
    assert row.before == {}
    assert row.requested["dimension_keys"] == ["targetName", "$.context.jwt.sub"]
    assert row.after["rateLimitId"] == "rl-1" and row.after["status"] == "CREATING"
    assert row.completed_at is not None


def test_create_accepts_camel_case_dimension_keys(client, control):
    body = _create_body()
    body["dimensionKeys"] = body.pop("dimension_keys")
    body.pop("description")
    response = client.post(BASE, json=body)
    assert response.status_code == 201, response.text
    assert "description" not in control.create_gateway_rate_limit.call_args.kwargs


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (_create_body(dimension_keys=["hostName"]), "dimension_key_unknown"),
        (_create_body(dimension_keys=["$.context.jwt."]), "dimension_key_unknown"),
        (
            _create_body(
                dimension_keys=["targetName", "targetName"],
                entries=[_entry(requests=[{"rate": 1, "period": "second"}])],
            ),
            "dimension_key_duplicate",
        ),
        (
            _create_body(dimension_keys=[f"$.context.jwt.c{i}" for i in range(11)]),
            "dimension_keys_count",
        ),
        (_create_body(dimension_keys=[]), "dimension_keys_count"),
        (
            _create_body(
                dimension_keys=["targetName", "toolName"],
                entries=[
                    {
                        "dimensions": {"targetName": "*", "toolName": "search"},
                        "requests": [{"rate": 1, "period": "second"}],
                    }
                ],
            ),
            "wildcard_not_trailing",
        ),
        (
            _create_body(
                entries=[
                    {
                        "dimensions": {"toolName": "search"},
                        "requests": [{"rate": 1, "period": "second"}],
                    }
                ]
            ),
            "entry_dimensions_mismatch",
        ),
        (
            _create_body(
                dimension_keys=["targetName"],
                entries=[
                    {
                        "dimensions": {"targetName": "a", "toolName": "b"},
                        "requests": [{"rate": 1, "period": "second"}],
                    }
                ],
            ),
            "entry_dimensions_mismatch",
        ),
        (_create_body(entries=[_entry()]), "entry_no_metric"),
        (_create_body(entries=[_entry(requests=[])]), "entry_no_metric"),
        (_create_body(entries=[]), "entries_count"),
        (
            _create_body(entries=[_entry(tokens=[{"rate": 1, "period": "second"}])]),
            "period_not_allowed",
        ),
        (
            _create_body(entries=[_entry(connections=[{"rate": 1, "period": "minute"}])]),
            "period_not_allowed",
        ),
        (
            _create_body(entries=[_entry(requests=[{"rate": 1, "period": "hour"}])]),
            "period_not_allowed",
        ),
        (
            _create_body(entries=[_entry(requests=[{"rate": 10_000_001, "period": "second"}])]),
            "rate_out_of_range",
        ),
        (
            _create_body(entries=[_entry(requests=[{"rate": -1, "period": "second"}])]),
            "rate_out_of_range",
        ),
        (
            _create_body(
                entries=[
                    _entry(
                        requests=[{"rate": 1, "period": "second"}, {"rate": 2, "period": "minute"}]
                    )
                ]
            ),
            "rate_config_count",
        ),
        (_create_body(description="x" * 513), "description_too_long"),
    ],
)
def test_create_validation_answers_422_before_any_aws_call(client, control, body, reason):
    response = client.post(BASE, json=body)

    assert response.status_code == 422, response.text
    payload = response.json()
    assert payload["code"] == "governance.rate_limit_invalid"
    assert payload["detail"]["reason"] == reason
    control.create_gateway_rate_limit.assert_not_called()
    control.get_gateway.assert_not_called()  # validation precedes even the managed check
    assert _journal() == []


def test_create_allows_rate_zero_and_ten_million_and_every_documented_key(client, control):
    keys = [
        "targetName",
        "toolName",
        "qualifiedModelId",
        "$.context.jwt.tenant_id",
        "$.context.iam.principal",
        "$.context.iam.sourceIdentity",
    ]
    response = client.post(
        BASE,
        json=_create_body(
            dimension_keys=keys,
            entries=[
                {
                    "dimensions": {key: "*" for key in keys},
                    "requests": [{"rate": 0, "period": "second"}],
                    "tokens": [{"rate": 10_000_000, "period": "minute"}],
                }
            ],
        ),
    )
    assert response.status_code == 201, response.text


def test_mutation_on_unmanaged_gateway_is_409_without_an_aws_mutation(client, control):
    control.list_tags_for_resource.return_value = {"tags": {}}

    create = client.post(BASE, json=_create_body())
    update = client.put(
        f"{BASE}/rl-1", json={"entries": [_entry(requests=[{"rate": 1, "period": "second"}])]}
    )
    delete = client.delete(f"{BASE}/rl-1")

    for response in (create, update, delete):
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "governance.gateway_not_managed"
    control.create_gateway_rate_limit.assert_not_called()
    control.update_gateway_rate_limit.assert_not_called()
    control.delete_gateway_rate_limit.assert_not_called()
    assert _journal() == []


def test_conflict_exception_maps_to_409_and_journals_the_failure(client, control):
    control.create_gateway_rate_limit.side_effect = ClientError(
        {"Error": {"Code": "ConflictException", "Message": "duplicate dimension keys"}},
        "CreateGatewayRateLimit",
    )

    response = client.post(BASE, json=_create_body())

    assert response.status_code == 409
    assert response.json()["code"] == "aws.conflict"
    assert "duplicate dimension keys" in response.json()["message"]
    rows = _journal()
    assert len(rows) == 1
    assert rows[0].operation == "rate_limit.create"
    assert rows[0].status == "failed"
    assert rows[0].after is None
    assert "duplicate dimension keys" in (rows[0].error or "")


# ---- update -----------------------------------------------------------------


def test_update_replaces_entries_and_journals_before_and_after(client, control):
    response = client.put(
        f"{BASE}/rl-1",
        json={
            "entries": [_entry("office-facts", requests=[{"rate": 5, "period": "second"}])],
            "description": "tightened",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "UPDATING"
    assert control.update_gateway_rate_limit.call_args.kwargs == {
        "gatewayIdentifier": GW,
        "rateLimitId": "rl-1",
        "entries": [
            {
                "dimensions": {"targetName": "office-facts"},
                "requests": [{"rate": 5.0, "period": "second"}],
            }
        ],
        "description": "tightened",
    }
    rows = _journal()
    assert len(rows) == 1
    assert rows[0].operation == "rate_limit.update"
    assert rows[0].status == "succeeded"
    assert rows[0].before["rateLimitId"] == "rl-1" and rows[0].before["status"] == "ACTIVE"
    assert rows[0].requested["description"] == "tightened"
    assert rows[0].after["status"] == "UPDATING"


def test_update_validates_entries_against_the_existing_dimension_keys(client, control):
    response = client.put(
        f"{BASE}/rl-1",
        json={
            "entries": [
                {"dimensions": {"toolName": "x"}, "requests": [{"rate": 5, "period": "second"}]}
            ]
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "entry_dimensions_mismatch"
    control.update_gateway_rate_limit.assert_not_called()


@pytest.mark.parametrize("field", ["dimension_keys", "dimensionKeys"])
def test_update_rejects_dimension_keys(client, control, field):
    response = client.put(
        f"{BASE}/rl-1",
        json={
            field: ["targetName"],
            "entries": [_entry(requests=[{"rate": 5, "period": "second"}])],
        },
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "dimension_keys_immutable"
    control.update_gateway_rate_limit.assert_not_called()
    assert _journal() == []


def test_update_wrong_period_is_422(client, control):
    response = client.put(
        f"{BASE}/rl-1",
        json={"entries": [_entry(tokens=[{"rate": 5, "period": "second"}])]},
    )
    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "period_not_allowed"


# ---- delete -----------------------------------------------------------------


def test_delete_calls_aws_with_both_identifiers_and_journals(client, control):
    response = client.delete(f"{BASE}/rl-1")

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted": True, "id": "rl-1", "status": "DELETING"}
    control.delete_gateway_rate_limit.assert_called_once_with(
        gatewayIdentifier=GW, rateLimitId="rl-1"
    )
    rows = _journal()
    assert len(rows) == 1
    assert rows[0].operation == "rate_limit.delete"
    assert rows[0].status == "succeeded"
    assert rows[0].before["rateLimitId"] == "rl-1"
    assert rows[0].requested == {"rate_limit_id": "rl-1"}
    assert rows[0].after == {"rateLimitId": "rl-1", "status": "DELETING"}


def test_delete_missing_rate_limit_is_404_envelope(client, control):
    control.get_gateway_rate_limit.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no such rate limit"}},
        "GetGatewayRateLimit",
    )
    control.delete_gateway_rate_limit.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no such rate limit"}},
        "DeleteGatewayRateLimit",
    )
    response = client.delete(f"{BASE}/rl-9")
    assert response.status_code == 404
    assert response.json()["code"] == "aws.not_found"
    rows = _journal()
    assert rows[0].status == "failed" and rows[0].before == {}


def test_audit_lists_rate_limit_mutations(client, control):
    client.post(BASE, json=_create_body())
    client.delete(f"{BASE}/rl-1")

    response = client.get(f"/api/governance/gateways/{GW}/audit")
    assert response.status_code == 200
    operations = [change["operation"] for change in response.json()["changes"]]
    assert operations == ["rate_limit.delete", "rate_limit.create"]


# ---- wrapper ----------------------------------------------------------------


def test_rate_limit_wrappers_pass_identifiers_explicitly():
    control = MagicMock()
    control.list_gateway_rate_limits.side_effect = [
        {"rateLimits": [{"rateLimitId": "a"}], "nextToken": "n"},
        {"rateLimits": [{"rateLimitId": "b"}]},
    ]
    assert [i["rateLimitId"] for i in policy.list_gateway_rate_limits(control, GW)] == ["a", "b"]

    policy.create_gateway_rate_limit(
        control, gateway_id=GW, dimension_keys=["targetName"], entries=[{"dimensions": {}}]
    )
    assert control.create_gateway_rate_limit.call_args.kwargs == {
        "gatewayIdentifier": GW,
        "dimensionKeys": ["targetName"],
        "entries": [{"dimensions": {}}],
    }
    policy.create_gateway_rate_limit(
        control,
        gateway_id=GW,
        dimension_keys=["targetName"],
        entries=[],
        client_token="short",
    )
    assert control.create_gateway_rate_limit.call_args.kwargs["clientToken"] == "launchpad-short"
    policy.update_gateway_rate_limit(control, gateway_id=GW, rate_limit_id="rl", entries=[])
    assert control.update_gateway_rate_limit.call_args.kwargs == {
        "gatewayIdentifier": GW,
        "rateLimitId": "rl",
        "entries": [],
    }
    policy.delete_gateway_rate_limit(control, gateway_id=GW, rate_limit_id="rl")
    control.delete_gateway_rate_limit.assert_called_once_with(
        gatewayIdentifier=GW, rateLimitId="rl"
    )
