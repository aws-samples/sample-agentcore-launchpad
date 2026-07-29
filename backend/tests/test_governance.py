"""Span normalizer, policy bootstrap builders, decision recorder."""

from typing import Any
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import app.routers.governance as gov
from app.core.db import SessionLocal
from app.models.ledger import PolicyDecision
from app.services import traces
from app.services.policy_bootstrap import (
    POLICIES,
    POLICY_ENGINE_NAME,
    ensure_gateway_traces,
    ensure_policy_engine,
    ensure_transaction_search,
    render_policy_statement,
)


def test_normalize_spans_categories_and_offsets():
    spans = [
        {"name": "chat global.anthropic.claude-sonnet-4-6",
         "startTimeUnixNano": 2_000_000, "endTimeUnixNano": 5_000_000},
        {"name": "mcp tools/call hr-database___get_employee",
         "startTimeUnixNano": 1_000_000, "endTimeUnixNano": 3_000_000},
        {"name": "Bedrock AgentCore.CreateEvent",
         "startTimeUnixNano": 6_000_000, "endTimeUnixNano": 7_000_000},
        {"name": "Bedrock AgentCore.AuthorizeAction",
         "startTimeUnixNano": 500_000, "endTimeUnixNano": 600_000},
    ]
    rows = traces.normalize_spans(spans)
    by_name = {r["name"]: r for r in rows}
    assert by_name["chat global.anthropic.claude-sonnet-4-6"]["category"] == "model"
    assert by_name["mcp tools/call hr-database___get_employee"]["category"] == "tool"
    assert by_name["Bedrock AgentCore.CreateEvent"]["category"] == "memory"
    assert by_name["Bedrock AgentCore.AuthorizeAction"]["category"] == "policy"
    # offsets relative to earliest span
    assert rows[0]["start_ms"] == 0.0
    assert by_name["chat global.anthropic.claude-sonnet-4-6"]["duration_ms"] == 3.0


def test_render_policy_statement_substitutes_arn():
    arn = "arn:aws:bedrock-agentcore:us-west-2:1:gateway/launchpad-gw-x"
    for spec in POLICIES:
        statement = render_policy_statement(spec["file"], arn)
        assert "__GATEWAY_ARN__" not in statement
        assert arn in statement
    payout = render_policy_statement("payout_admin_only.cedar", arn)
    assert 'AgentCore::Action::"hr-database___create_payout"' in payout
    assert "platform-admin" in payout


def test_ensure_policy_engine_idempotent():
    control = MagicMock()
    control.list_policy_engines.return_value = {
        "policyEngines": [{"name": "launchpad_pe", "policyEngineId": "pe-1",
                           "policyEngineArn": "arn:pe-1"}]
    }
    engine, created = ensure_policy_engine(control)
    assert created is False and engine["id"] == "pe-1"
    control.create_policy_engine.assert_not_called()


def test_ensure_transaction_search_noop_when_active():
    xray = MagicMock()
    xray.get_trace_segment_destination.return_value = {
        "Destination": "CloudWatchLogs", "Status": "ACTIVE",
    }
    state = ensure_transaction_search(xray)
    assert state == {"enabled": True, "changed": False, "status": "ACTIVE"}
    xray.update_trace_segment_destination.assert_not_called()


# ── gateway TRACES delivery (Policy span channel) ───────────────────────────

GW_ID = "launchpad-gw-em0yuqmmdp"
GW_ARN = f"arn:aws:bedrock-agentcore:us-west-2:434444145045:gateway/{GW_ID}"
SOURCE = f"{GW_ID}-traces-source"
DEST = f"{GW_ID}-traces-destination"
DEST_ARN = f"arn:aws:logs:us-west-2:434444145045:delivery-destination:{DEST}"


def _not_found(op: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "absent"}}, op
    )


class FakeLogs:
    """Records calls so idempotency is assertable, unlike a bare MagicMock."""

    def __init__(
        self,
        *,
        has_source: bool = False,
        has_destination: bool = False,
        has_delivery: bool = False,
    ) -> None:
        self.has_source = has_source
        self.has_destination = has_destination
        self.has_delivery = has_delivery
        self.calls: list[str] = []

    def get_delivery_source(self, name: str) -> dict[str, Any]:
        self.calls.append("get_delivery_source")
        if not self.has_source:
            raise _not_found("GetDeliverySource")
        return {"deliverySource": {"name": name, "logType": "TRACES"}}

    def get_delivery_destination(self, name: str) -> dict[str, Any]:
        self.calls.append("get_delivery_destination")
        if not self.has_destination:
            raise _not_found("GetDeliveryDestination")
        return {"deliveryDestination": {"name": name, "arn": DEST_ARN}}

    def put_delivery_source(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("put_delivery_source")
        assert kwargs["logType"] == "TRACES"
        assert kwargs["resourceArn"] == GW_ARN
        self.has_source = True
        return {"deliverySource": {"name": kwargs["name"]}}

    def put_delivery_destination(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("put_delivery_destination")
        assert kwargs["deliveryDestinationType"] == "XRAY"
        # XRAY destinations take no deliveryDestinationConfiguration.
        assert "deliveryDestinationConfiguration" not in kwargs
        self.has_destination = True
        return {"deliveryDestination": {"name": kwargs["name"], "arn": DEST_ARN}}

    def describe_deliveries(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("describe_deliveries")
        if not self.has_delivery:
            return {"deliveries": []}
        return {"deliveries": [{"id": "dlv-1", "deliverySourceName": SOURCE}]}

    def create_delivery(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("create_delivery")
        assert kwargs["deliverySourceName"] == SOURCE
        assert kwargs["deliveryDestinationArn"] == DEST_ARN
        self.has_delivery = True
        return {"delivery": {"id": "dlv-1"}}


def _traces(logs: Any, enabled: bool = True) -> dict[str, Any]:
    return ensure_gateway_traces(
        logs, GW_ARN, GW_ID, transaction_search_enabled=enabled
    )


def test_gateway_traces_created_on_fresh_account():
    logs = FakeLogs()
    result = _traces(logs)
    assert result["status"] == "created" and result["changed"] is True
    assert result["source"] == SOURCE and result["destination"] == DEST
    assert result["delivery_id"] == "dlv-1"
    assert logs.calls.count("put_delivery_source") == 1
    assert logs.calls.count("put_delivery_destination") == 1
    assert logs.calls.count("create_delivery") == 1


def test_gateway_traces_rerun_makes_no_writes():
    """`make bootstrap` is re-runnable; a second run must not touch anything."""
    logs = FakeLogs(has_source=True, has_destination=True, has_delivery=True)
    result = _traces(logs)
    assert result["status"] == "present" and result["changed"] is False
    assert "put_delivery_source" not in logs.calls
    assert "put_delivery_destination" not in logs.calls
    assert "create_delivery" not in logs.calls


def test_gateway_traces_creates_only_the_missing_delivery():
    logs = FakeLogs(has_source=True, has_destination=True, has_delivery=False)
    result = _traces(logs)
    assert result["status"] == "created"
    assert logs.calls.count("create_delivery") == 1
    assert "put_delivery_source" not in logs.calls
    assert "put_delivery_destination" not in logs.calls


def test_gateway_traces_conflict_is_treated_as_present():
    class Conflicting(FakeLogs):
        def create_delivery(self, **kwargs: Any) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "ConflictException", "Message": "exists"}},
                "CreateDelivery",
            )

    result = _traces(Conflicting(has_source=True, has_destination=True))
    assert result["status"] == "present" and result["changed"] is False


def test_gateway_traces_skipped_without_transaction_search():
    logs = FakeLogs()
    result = _traces(logs, enabled=False)
    assert result["status"] == "skipped"
    assert result["reason"] == "transaction_search_disabled"
    assert logs.calls == []  # no AWS call attempted at all


def test_gateway_traces_failure_is_reported_not_raised():
    class Denied(FakeLogs):
        def get_delivery_source(self, name: str) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "GetDeliverySource",
            )

    result = _traces(Denied())
    assert result["status"] == "failed"
    assert result["reason"] == "AccessDeniedException"


def test_gateway_traces_never_touches_the_gateway_resource():
    """Structural guard: enabling traces must not mutate the Gateway. The function
    takes no control-plane client, so an UpdateGateway call cannot be added
    without changing this signature and this test."""
    import inspect

    params = set(inspect.signature(ensure_gateway_traces).parameters)
    assert "control" not in params
    assert params == {"logs", "gateway_arn", "gateway_id", "transaction_search_enabled"}

    logs = FakeLogs()
    _traces(logs)
    assert not any("gateway" in call for call in logs.calls)


def test_run_policy_bootstrap_requires_logs_and_reports_traces():
    """`logs` is keyword-only and required, so a caller that forgets to wire it
    fails loudly instead of silently skipping the span channel."""
    import inspect

    from app.services.policy_bootstrap import run_policy_bootstrap

    param = inspect.signature(run_policy_bootstrap).parameters["logs"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty

    control = MagicMock()
    control.list_policy_engines.return_value = {
        "policyEngines": [
            {"name": POLICY_ENGINE_NAME, "policyEngineId": "pe-1", "policyEngineArn": "arn:pe-1"}
        ]
    }
    control.list_policies.return_value = {
        "policies": [
            {"name": spec["name"], "policyId": f"p-{i}"}
            for i, spec in enumerate(POLICIES)
        ]
    }
    control.get_gateway.return_value = {
        "policyEngineConfiguration": {"arn": "arn:pe-1", "mode": "ENFORCE"},
    }
    xray = MagicMock()
    xray.get_trace_segment_destination.return_value = {
        "Destination": "CloudWatchLogs", "Status": "ACTIVE",
    }
    logs = FakeLogs()

    summary = run_policy_bootstrap(
        control,
        xray,
        {"resources": {"gateway_arn": GW_ARN, "gateway_id": GW_ID}},
        logs=logs,
    )
    assert summary["gateway_traces"]["status"] == "created"
    # steady state elsewhere: no Gateway mutation from the attach step either
    control.update_gateway.assert_not_called()


def test_unexpected_client_error_in_existence_check_surfaces():
    """_absent must only swallow ResourceNotFoundException; a throttle is real."""
    from app.services.policy_bootstrap import _absent

    def throttled(**kwargs: Any) -> None:
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Get"
        )

    with pytest.raises(ClientError):
        _absent(throttled, name="x")


def test_policy_test_records_decision(client, monkeypatch):
    monkeypatch.setattr(
        gov.mcp_client, "tools_call",
        lambda tool, args, username="demo": {"content": [{"text": "ok"}]},
    )
    res = client.post(
        "/api/governance/policy-test",
        json={"username": "river", "tool": "hr-database___create_payout",
              "arguments": {"employee_id": "EMP-1024", "amount": 1}},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["outcome"] == "ALLOW" and body["principal"] == "river@platform-admin"

    from app.core.errors import AppError

    def deny(tool, args, username="demo"):
        raise AppError("gateway.rpc_error", "Tool Execution Denied", {"policy": "R-02"})

    monkeypatch.setattr(gov.mcp_client, "tools_call", deny)
    res2 = client.post(
        "/api/governance/policy-test",
        json={"username": "demo", "tool": "hr-database___create_payout",
              "arguments": {}},
    )
    assert res2.json()["outcome"] == "DENY"

    log = client.get("/api/governance/decisions").json()["decisions"]
    assert len(log) == 2
    assert log[0]["outcome"] == "DENY" and log[0]["principal"] == "demo@hr-analyst"

    db = SessionLocal()
    assert db.query(PolicyDecision).count() == 2
    db.close()
