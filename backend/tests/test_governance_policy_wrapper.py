from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from app.services.agentcore import policy


def test_gateway_and_policy_pagination():
    control = MagicMock()
    control.list_gateways.side_effect = [
        {"items": [{"gatewayId": "gw-1"}], "nextToken": "next"},
        {"items": [{"gatewayId": "gw-2"}]},
    ]
    control.list_policies.side_effect = [
        {"policies": [{"policyId": "p-1"}], "nextToken": "next"},
        {"policies": [{"policyId": "p-2"}]},
    ]

    assert [item["gatewayId"] for item in policy.list_gateways(control)] == [
        "gw-1",
        "gw-2",
    ]
    assert [item["policyId"] for item in policy.list_policies(control, "pe-1")] == [
        "p-1",
        "p-2",
    ]
    assert control.list_gateways.call_args_list[1].kwargs["nextToken"] == "next"
    assert control.list_policies.call_args_list[1].kwargs["policyEngineId"] == "pe-1"


def test_management_tags_preserve_unrelated_tags():
    control = MagicMock()
    control.list_tags_for_resource.return_value = {
        "tags": {"owner": "platform", **policy.MANAGED_TAGS}
    }
    assert policy.is_managed(policy.list_tags(control, "arn:gateway"))

    policy.tag_managed(control, "arn:gateway")
    control.tag_resource.assert_called_once_with(
        resourceArn="arn:gateway",
        tags=policy.MANAGED_TAGS,
    )
    policy.untag_managed(control, "arn:gateway")
    control.untag_resource.assert_called_once_with(
        resourceArn="arn:gateway",
        tagKeys=list(policy.MANAGED_TAGS),
    )


def test_update_gateway_preserves_supported_fields():
    now = datetime.now(UTC)
    gateway = {
        "gatewayId": "gw-1",
        "gatewayArn": "arn:gw-1",
        "gatewayUrl": "https://example.test/mcp",
        "name": "existing-gateway",
        "roleArn": "arn:aws:iam::123:role/gateway",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": {
            "customJWTAuthorizer": {"discoveryUrl": "https://issuer.test/.well-known"}
        },
        "description": "keep",
        "protocolType": "MCP",
        "protocolConfiguration": {"mcp": {"supportedVersions": ["2025-06-18"]}},
        "kmsKeyArn": "arn:kms",
        "customTransformConfiguration": {"lambda": {"arn": "arn:lambda:transform"}},
        "interceptorConfigurations": [
            {
                "interceptor": {"lambda": {"arn": "arn:lambda:interceptor"}},
                "interceptionPoints": ["REQUEST"],
            }
        ],
        "exceptionLevel": "DEBUG",
        "wafConfiguration": {"failureMode": "FAIL_CLOSE"},
        "policyEngineConfiguration": {"arn": "arn:old", "mode": "ENFORCE"},
        "createdAt": now,
        "updatedAt": now,
        "status": "READY",
        "statusReasons": [],
        "workloadIdentityDetails": {"workloadIdentityArn": "arn:identity"},
        "webAclArn": "arn:waf",
    }
    params = policy.gateway_update_params(
        gateway,
        {"arn": "arn:new", "mode": "LOG_ONLY"},
    )

    assert params["policyEngineConfiguration"] == {
        "arn": "arn:new",
        "mode": "LOG_ONLY",
    }
    for field in (
        "name",
        "roleArn",
        "authorizerType",
        "authorizerConfiguration",
        "description",
        "protocolType",
        "protocolConfiguration",
        "kmsKeyArn",
        "customTransformConfiguration",
        "interceptorConfigurations",
        "exceptionLevel",
        "wafConfiguration",
    ):
        assert params[field] == gateway[field]
    assert "status" not in params
    assert "gatewayArn" not in params
    assert "workloadIdentityDetails" not in params
    assert "webAclArn" not in params


def test_update_gateway_fetches_fresh_state():
    control = MagicMock()
    control.get_gateway.return_value = {
        "gatewayId": "gw-1",
        "name": "gw",
        "roleArn": "arn:role",
        "authorizerType": "AWS_IAM",
        "description": "current",
    }
    policy.update_gateway_policy_configuration(
        control,
        gateway_id="gw-1",
        engine_arn="arn:engine",
        mode="LOG_ONLY",
    )
    control.get_gateway.assert_called_once_with(gatewayIdentifier="gw-1")
    assert control.update_gateway.call_args.kwargs["description"] == "current"


def test_wait_policy_surfaces_terminal_failure():
    control = MagicMock()
    control.get_policy.return_value = {
        "status": "UPDATE_FAILED",
        "statusReasons": ["invalid Cedar"],
    }
    with pytest.raises(RuntimeError, match="invalid Cedar"):
        policy.wait_policy_active(
            control,
            "pe-1",
            "p-1",
            interval_s=0,
            sleeper=lambda _: None,
        )


def test_wait_gateway_configuration_ignores_stale_ready_snapshot():
    control = MagicMock()
    control.get_gateway.side_effect = [
        {
            "status": "READY",
            "policyEngineConfiguration": {"arn": "arn:engine", "mode": "ENFORCE"},
        },
        {
            "status": "READY",
            "policyEngineConfiguration": {"arn": "arn:engine", "mode": "LOG_ONLY"},
        },
    ]

    settled = policy.wait_gateway_policy_configuration(
        control,
        "gw-1",
        engine_arn="arn:engine",
        mode="LOG_ONLY",
        interval_s=0,
        sleeper=lambda _: None,
    )

    assert settled["policyEngineConfiguration"]["mode"] == "LOG_ONLY"
    assert control.get_gateway.call_count == 2


def test_wait_gateway_configuration_ignores_wrong_engine_arn():
    control = MagicMock()
    control.get_gateway.side_effect = [
        {
            "status": "READY",
            "policyEngineConfiguration": {"arn": "arn:old-engine", "mode": "LOG_ONLY"},
        },
        {
            "status": "READY",
            "policyEngineConfiguration": {"arn": "arn:engine", "mode": "LOG_ONLY"},
        },
    ]

    settled = policy.wait_gateway_policy_configuration(
        control,
        "gw-1",
        engine_arn="arn:engine",
        mode="LOG_ONLY",
        interval_s=0,
        sleeper=lambda _: None,
    )

    assert settled["policyEngineConfiguration"]["arn"] == "arn:engine"
    assert control.get_gateway.call_count == 2


def test_wait_gateway_configuration_waits_for_ready_after_transition():
    control = MagicMock()
    control.get_gateway.side_effect = [
        {
            "status": "UPDATING",
            "policyEngineConfiguration": {"arn": "arn:engine", "mode": "LOG_ONLY"},
        },
        {
            "status": "READY",
            "policyEngineConfiguration": {"arn": "arn:engine", "mode": "LOG_ONLY"},
        },
    ]

    settled = policy.wait_gateway_policy_configuration(
        control,
        "gw-1",
        engine_arn="arn:engine",
        mode="LOG_ONLY",
        interval_s=0,
        sleeper=lambda _: None,
    )

    assert settled["status"] == "READY"


def test_wait_gateway_configuration_accepts_exact_ready_snapshot():
    control = MagicMock()
    control.get_gateway.return_value = {
        "status": "READY",
        "policyEngineConfiguration": {"arn": "arn:engine", "mode": "ENFORCE"},
    }

    settled = policy.wait_gateway_policy_configuration(
        control,
        "gw-1",
        engine_arn="arn:engine",
        mode="ENFORCE",
    )

    assert settled["policyEngineConfiguration"] == {
        "arn": "arn:engine",
        "mode": "ENFORCE",
    }


def test_wait_gateway_configuration_surfaces_terminal_failure():
    control = MagicMock()
    control.get_gateway.return_value = {
        "status": "UPDATE_UNSUCCESSFUL",
        "statusReasons": ["invalid configuration"],
    }

    with pytest.raises(RuntimeError, match="invalid configuration"):
        policy.wait_gateway_policy_configuration(
            control,
            "gw-1",
            engine_arn="arn:engine",
            mode="LOG_ONLY",
            interval_s=0,
            sleeper=lambda _: None,
        )


def test_wait_gateway_configuration_times_out_on_wrong_ready_snapshot(monkeypatch):
    control = MagicMock()
    control.get_gateway.return_value = {
        "status": "READY",
        "policyEngineConfiguration": {"arn": "arn:engine", "mode": "ENFORCE"},
    }
    monotonic = MagicMock(side_effect=[0, 1])
    monkeypatch.setattr(policy.time, "monotonic", monotonic)

    with pytest.raises(TimeoutError, match="did not reach READY"):
        policy.wait_gateway_policy_configuration(
            control,
            "gw-1",
            engine_arn="arn:engine",
            mode="LOG_ONLY",
            timeout_s=0,
            interval_s=0,
            sleeper=lambda _: None,
        )


def test_create_policy_is_always_log_only():
    control = MagicMock()
    policy.create_policy(
        control,
        engine_id="pe-1",
        name="candidate",
        statement="permit(principal, action, resource);",
    )
    assert control.create_policy.call_args.kwargs["enforcementMode"] == "LOG_ONLY"
    assert (
        control.create_policy.call_args.kwargs["validationMode"]
        == "FAIL_ON_ANY_FINDINGS"
    )


def test_short_client_tokens_are_normalized_for_agentcore():
    control = MagicMock()
    short_token = "a" * 32
    expected = f"launchpad-{short_token}"

    policy.create_policy_engine(
        control,
        name="engine",
        client_token=short_token,
    )
    policy.create_policy(
        control,
        engine_id="pe-1",
        name="candidate",
        statement="permit(principal, action, resource);",
        client_token=short_token,
    )
    policy.start_policy_generation(
        control,
        engine_id="pe-1",
        gateway_arn="arn:aws:bedrock-agentcore:us-west-2:123:gateway/gw-1",
        name="generated",
        text="Allow the support tool for the demo principal.",
        client_token=short_token,
    )

    assert control.create_policy_engine.call_args.kwargs["clientToken"] == expected
    assert control.create_policy.call_args.kwargs["clientToken"] == expected
    assert control.start_policy_generation.call_args.kwargs["clientToken"] == expected
