"""N17-obs474 (security, user-approved) — the platform gateway must not run with
``exceptionLevel: DEBUG``.

r17-bn's P0 probe: with DEBUG, an interceptor / target Lambda error returns the Lambda's
``errorMessage`` AND stack trace to the CALLER — the agent, which may be prompt-injected.
``gateway_bootstrap.ensure_gateway`` created ``launchpad-gw`` / ``launchpad-gw-t1`` that way.
New gateways are created without it (the service model's only value is ``DEBUG``; omitting
it is the non-debug level), and existing ones are repaired by ``repair_exception_level``:
``UpdateGateway`` REPLACES the configuration (an omitted optional field resets), so the
update carries every field ``GetGateway`` reports and changes only ``exceptionLevel`` —
verified by reading the gateway back."""

from copy import deepcopy
from unittest.mock import MagicMock

import botocore.session
import pytest

from app.services import gateway_bootstrap as gb

GW = "gw-1234567890"
AC = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012"
ACCT = "cn-northwest-1:123456789012"
DETAIL = {
    "ResponseMetadata": {"HTTPStatusCode": 200, "RequestId": "r-1"},
    "gatewayArn": f"{AC}:gateway/{GW}",
    "gatewayId": GW,
    "gatewayUrl": f"https://{GW}.gateway.bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/mcp",
    "createdAt": "2026-10-01T00:00:00Z",
    "updatedAt": "2026-10-20T00:00:00Z",
    "status": "READY",
    "statusReasons": [],
    "name": "launchpad-gw-t1",
    "description": "AgentCore Launchpad shared MCP gateway",
    "roleArn": "arn:aws-cn:iam::123456789012:role/launchpad-gateway-t1",
    "protocolType": "MCP",
    "protocolConfiguration": {"mcp": {"supportedVersions": ["2025-03-26"],
                                      "searchType": "SEMANTIC"}},
    "authorizerType": "AWS_IAM",
    "kmsKeyArn": f"arn:aws-cn:kms:{ACCT}:key/k-1",
    "interceptorConfigurations": [{
        "interceptor": {"lambda": {"arn": f"arn:aws-cn:lambda:{ACCT}:function:lp-guard"}},
        "interceptionPoints": ["REQUEST"],
        "inputConfiguration": {"passRequestHeaders": False},
    }],
    "policyEngineConfiguration": {"arn": f"{AC}:policy-engine/pe-1", "mode": "ENFORCE"},
    "workloadIdentityDetails": {"workloadIdentityArn": f"{AC}:workload-identity/w"},
    "exceptionLevel": "DEBUG",
}


def _expected_payload(detail: dict) -> dict:
    """GetGateway minus exceptionLevel, minus the output-only fields, plus the identifier."""
    out = {k: deepcopy(v) for k, v in detail.items() if k in gb.GATEWAY_UPDATE_FIELDS}
    return {"gatewayIdentifier": detail["gatewayId"], **out}


def _control(before: dict, after: dict | None = None) -> MagicMock:
    control = MagicMock()
    reads = [deepcopy(before)]
    if after is not None:
        reads += [dict(deepcopy(after), status="UPDATING"), deepcopy(after)]
    control.get_gateway.side_effect = (
        lambda **_: reads.pop(0) if len(reads) > 1 else deepcopy(reads[0]))
    control.update_gateway.return_value = {"gatewayId": before["gatewayId"], "status": "UPDATING"}
    return control


def _repaired(detail: dict) -> dict:
    out = deepcopy(detail)
    out.pop("exceptionLevel")
    return out


def test_new_gateways_are_created_without_debug(monkeypatch):
    monkeypatch.setattr(gb.time, "sleep", lambda _s: None)
    control = MagicMock()
    control.list_gateways.return_value = {"items": []}
    control.create_gateway.return_value = {"gatewayId": GW}
    control.get_gateway.return_value = _repaired(DETAIL)
    gb.ensure_gateway(control, role_arn=DETAIL["roleArn"], region="cn-northwest-1")
    assert "exceptionLevel" not in control.create_gateway.call_args.kwargs


def test_the_carried_fields_are_exactly_what_update_gateway_accepts():
    # an SDK that adds an UpdateGateway field fails this, so nothing is ever reset silently
    model = botocore.session.get_session().get_service_model("bedrock-agentcore-control")
    members = set(model.operation_model("UpdateGateway").input_shape.members)
    assert set(gb.GATEWAY_UPDATE_FIELDS) == members - {"gatewayIdentifier", "exceptionLevel"}
    assert model.shape_for("ExceptionLevel").enum == ["DEBUG"]  # omitting it IS the non-debug level


def test_the_update_payload_is_get_gateway_minus_exception_level():
    payload = gb.gateway_update_payload(DETAIL)
    assert payload == _expected_payload(DETAIL)
    assert "exceptionLevel" not in payload
    for output_only in ("ResponseMetadata", "gatewayArn", "gatewayUrl", "status", "createdAt",
                        "workloadIdentityDetails"):
        assert output_only not in payload
    # every field GetGateway reported (other than the output-only ones) is carried unchanged
    assert payload["interceptorConfigurations"] == DETAIL["interceptorConfigurations"]
    assert payload["policyEngineConfiguration"] == DETAIL["policyEngineConfiguration"]
    assert payload["kmsKeyArn"] == DETAIL["kmsKeyArn"]


def test_a_dry_run_reports_and_changes_nothing():
    control = _control(DETAIL)
    report = gb.repair_exception_level(control, GW, apply=False)
    control.update_gateway.assert_not_called()
    assert report["action"] == "would_update" and report["before"] == "DEBUG"
    assert report["payload"] == _expected_payload(DETAIL)


def test_apply_updates_once_and_verifies_only_exception_level_changed(monkeypatch):
    monkeypatch.setattr(gb.time, "sleep", lambda _s: None)
    control = _control(DETAIL, after=_repaired(DETAIL))
    report = gb.repair_exception_level(control, GW, apply=True)
    control.update_gateway.assert_called_once()
    assert control.update_gateway.call_args.kwargs == _expected_payload(DETAIL)
    assert report["action"] == "updated"
    assert report["after"] is None and report["unchanged_fields_verified"] is True


def test_an_already_safe_gateway_is_left_alone():
    control = _control(_repaired(DETAIL))
    report = gb.repair_exception_level(control, GW, apply=True)
    control.update_gateway.assert_not_called()
    assert report["action"] == "none" and report["before"] is None


def test_a_read_back_that_changed_another_field_is_refused(monkeypatch):
    monkeypatch.setattr(gb.time, "sleep", lambda _s: None)
    drifted = _repaired(DETAIL)
    drifted["interceptorConfigurations"] = []  # the update reset it — must never pass silently
    control = _control(DETAIL, after=drifted)
    with pytest.raises(RuntimeError, match="interceptorConfigurations"):
        gb.repair_exception_level(control, GW, apply=True)


def test_a_read_back_still_at_debug_is_refused(monkeypatch):
    monkeypatch.setattr(gb.time, "sleep", lambda _s: None)
    control = _control(DETAIL, after=DETAIL)
    with pytest.raises(RuntimeError, match="exceptionLevel"):
        gb.repair_exception_level(control, GW, apply=True)


def test_the_repair_script_never_targets_the_probe_gateway():
    from scripts import repair_gateway_exception_level as script

    assert script.target_allowed("launchpad-gw") and script.target_allowed("launchpad-gw-t1")
    assert not script.target_allowed("launchpad-probe-gw")
    assert not script.target_allowed("someone-elses-gw")
    # --probe: exactly the kept P0 probe gateway, nothing else
    assert script.target_allowed("launchpad-probe-gw", probe=True)
    assert not script.target_allowed("launchpad-gw", probe=True)
    assert not script.target_allowed("launchpad-probe-gw-2", probe=True)
