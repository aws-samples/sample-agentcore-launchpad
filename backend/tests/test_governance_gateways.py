from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

from app.core.config import Settings
from app.services import governance
from app.services.agentcore import gateway as gateway_api

from .conftest import ws_ctx


def _gateway(
    gateway_id: str,
    name: str,
    *,
    engine_arn: str | None = None,
    authorizer: str = "AWS_IAM",
) -> dict:
    value = {
        "gatewayId": gateway_id,
        "gatewayArn": f"arn:aws:bedrock-agentcore:us-west-2:123:gateway/{gateway_id}",
        "gatewayUrl": f"https://{gateway_id}.example.test/mcp",
        "name": name,
        "status": "READY",
        "statusReasons": [],
        "updatedAt": datetime.now(UTC),
        "protocolType": "MCP",
        "authorizerType": authorizer,
        "roleArn": "arn:aws:iam::123:role/gateway",
    }
    if engine_arn:
        value["policyEngineConfiguration"] = {
            "arn": engine_arn,
            "mode": "LOG_ONLY",
        }
    return value


def _control(gateways: list[dict]) -> MagicMock:
    control = MagicMock()
    control.list_gateways.return_value = {
        "items": [
            {
                "gatewayId": gateway["gatewayId"],
                "name": gateway["name"],
                "protocolType": "MCP",
            }
            for gateway in gateways
        ]
    }
    by_id = {gateway["gatewayId"]: gateway for gateway in gateways}
    control.get_gateway.side_effect = lambda gatewayIdentifier: by_id[gatewayIdentifier]
    control.list_tags_for_resource.return_value = {"tags": {}}
    control.list_gateway_targets.return_value = {"items": []}
    control.list_registry_records.return_value = {"registryRecords": []}
    control.get_policy_engine.return_value = {
        "policyEngineId": "pe-1",
        "policyEngineArn": "arn:engine/pe-1",
        "name": "shared",
        "status": "ACTIVE",
        "updatedAt": datetime.now(UTC),
    }
    return control


def test_manage_and_unmanage_touch_only_launchpad_tags():
    control = _control([_gateway("gw-1", "alpha")])
    governance.manage_gateway(control, "gw-1")
    control.tag_resource.assert_called_once_with(
        resourceArn="arn:aws:bedrock-agentcore:us-west-2:123:gateway/gw-1",
        tags=gateway_api.MANAGED_TAGS,
    )

    governance.unmanage_gateway(control, "gw-1")
    control.untag_resource.assert_called_once_with(
        resourceArn="arn:aws:bedrock-agentcore:us-west-2:123:gateway/gw-1",
        tagKeys=list(gateway_api.MANAGED_TAGS),
    )
    control.update_gateway.assert_not_called()


def test_attachability_has_no_oauth_arm_for_custom_jwt_gateways():
    """N8: the CN build writes no `oauth_provider_arn` and no runtime honours an OAuth
    gateway, so a CUSTOM_JWT authorizer is catalog-only even on the workspace's own gateway
    — a stale `oauth_provider_arn` key in the resource map changes nothing."""
    launchpad = _gateway("managed", "launchpad-gw", authorizer="CUSTOM_JWT")
    external = _gateway("external", "partner-gw", authorizer="CUSTOM_JWT")
    control = _control([launchpad, external])
    governance.invalidate_gateway_cache()
    views = governance.list_gateway_views(
        control,
        ws_ctx({
                "gateway_id": "managed",
                "gateway_url": launchpad["gatewayUrl"],
                "oauth_provider_arn": "arn:oauth",  # stale key: must be ignored
                "registry_id": "",
            }),
        refresh=True,
    )
    by_id = {view["id"]: view for view in views}
    unmanaged = {
        "attachable": False,
        "reason": "custom_jwt_provider_unmanaged",
        "auth_type": "oauth",
    }
    assert by_id["managed"]["attachability"] == unmanaged
    assert by_id["managed"]["policy_test_available"] is True  # independent of attachability
    assert by_id["external"]["attachability"] == unmanaged
    assert by_id["external"]["policy_test_available"] is False
    assert "oauth_provider_arn" not in Path(governance.__file__).read_text(encoding="utf-8")


def test_policy_test_availability_requires_configured_gateway_id_and_url():
    launchpad = _gateway("managed", "launchpad-gw", authorizer="CUSTOM_JWT")
    control = _control([launchpad])

    for resources, expected in [
        (
            {
                "gateway_id": launchpad["gatewayId"],
                "gateway_url": launchpad["gatewayUrl"],
                "registry_id": "",
            },
            True,
        ),
        (
            {
                "gateway_id": launchpad["gatewayId"],
                "gateway_url": "https://different.example.test/mcp",
                "registry_id": "",
            },
            False,
        ),
        (
            {
                "gateway_id": "different",
                "gateway_url": launchpad["gatewayUrl"],
                "registry_id": "",
            },
            False,
        ),
    ]:
        governance.invalidate_gateway_cache()
        [view] = governance.list_gateway_views(
            control,
            ws_ctx(resources),
            refresh=True,
        )
        assert view["policy_test_available"] is expected


def test_external_custom_jwt_detail_offers_token_free_tools_list_template():
    launchpad = _gateway("managed", "launchpad-gw", authorizer="CUSTOM_JWT")
    external = _gateway("external", "partner-gw", authorizer="CUSTOM_JWT")
    iam_gateway = _gateway("iam", "internal-gw")
    control = _control([launchpad, external, iam_gateway])
    settings = Settings(
        resources={
            "gateway_id": "managed",
            "registry_id": "",
        }
    )

    command = governance.gateway_detail(
        control,
        "external",
        ws_ctx(settings.resources),
    )["external_tools_list_command"]
    assert "tools/list" in command
    assert "https://external.example.test/mcp" in command
    # the template must reference an operator-side variable, never a real token
    assert "$GATEWAY_ACCESS_TOKEN" in command

    for gateway_id in ("managed", "iam"):
        assert (
            governance.gateway_detail(
                control,
                gateway_id,
                ws_ctx(settings.resources),
            )["external_tools_list_command"]
            is None
        )


def test_action_discovery_uses_exact_control_plane_names():
    actions = governance.discover_actions(
        [
            {
                "targetId": "t-1",
                "name": "hr",
                "targetConfiguration": {
                    "mcp": {
                        "lambda": {
                            "toolSchema": {
                                "inlinePayload": [
                                    {
                                        "name": "get_employee",
                                        "description": "Get employee",
                                        "inputSchema": {"type": "object"},
                                    }
                                ]
                            }
                        }
                    }
                },
            },
            {
                "targetId": "t-2",
                "name": "facts",
                "targetConfiguration": {
                    "mcp": {
                        "openApiSchema": {
                            "inlinePayload": (
                                '{"paths":{"/facts":{"get":'
                                '{"operationId":"list_facts"}}}}'
                            )
                        }
                    }
                },
            },
        ]
    )
    assert [action["name"] for action in actions] == [
        "facts___list_facts",
        "hr___get_employee",
    ]
    assert all(action["verified"] for action in actions)


def test_launchpad_gateway_detail_shows_aws_iam_clearly():
    """The shared launchpad-gw is always AWS_IAM since N1; gateway_detail's
    generic authorizer_type field already surfaces that with no special-casing
    needed (R6) — this pins the display so a future change notices."""
    launchpad = _gateway("managed", "launchpad-gw")  # authorizer defaults to AWS_IAM
    control = _control([launchpad])

    detail = governance.gateway_detail(
        control,
        "managed",
        ws_ctx({
            "gateway_id": "managed",
            "gateway_url": launchpad["gatewayUrl"],
            "registry_id": "",
        }),
    )
    assert detail["authorizer_type"] == "AWS_IAM"
    assert detail["attachability"] == {"attachable": True, "reason": None, "auth_type": "aws_iam"}
    assert detail["authorizer_configuration"] is None


