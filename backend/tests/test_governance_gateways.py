from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from botocore.exceptions import ClientError

from app.core.config import Settings
from app.services import governance
from app.services.agentcore import policy


class _NotFound(Exception):
    """Stands in for the preview SDK's ResourceNotFoundException."""


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


def test_discovery_is_read_only_and_reports_shared_engine():
    engine_arn = "arn:aws:bedrock-agentcore:us-west-2:123:policy-engine/pe-1"
    control = _control(
        [
            _gateway("gw-1", "alpha", engine_arn=engine_arn),
            _gateway("gw-2", "beta", engine_arn=engine_arn),
        ]
    )
    governance.invalidate_gateway_cache()
    views = governance.list_gateway_views(
        control,
        settings=Settings(resources={"registry_id": ""}),
        refresh=True,
    )

    assert len(views) == 2
    assert views[0]["shared_engine"] is True
    assert {item["id"] for item in views[0]["shared_gateways"]} == {"gw-1", "gw-2"}
    control.tag_resource.assert_not_called()
    control.untag_resource.assert_not_called()
    control.update_gateway.assert_not_called()


def test_manage_and_unmanage_touch_only_launchpad_tags():
    control = _control([_gateway("gw-1", "alpha")])
    governance.manage_gateway(control, "gw-1")
    control.tag_resource.assert_called_once_with(
        resourceArn="arn:aws:bedrock-agentcore:us-west-2:123:gateway/gw-1",
        tags=policy.MANAGED_TAGS,
    )

    governance.unmanage_gateway(control, "gw-1")
    control.untag_resource.assert_called_once_with(
        resourceArn="arn:aws:bedrock-agentcore:us-west-2:123:gateway/gw-1",
        tagKeys=list(policy.MANAGED_TAGS),
    )
    control.update_gateway.assert_not_called()


def test_attachability_keeps_external_custom_jwt_catalog_only():
    launchpad = _gateway("managed", "launchpad-gw", authorizer="CUSTOM_JWT")
    external = _gateway("external", "partner-gw", authorizer="CUSTOM_JWT")
    control = _control([launchpad, external])
    governance.invalidate_gateway_cache()
    views = governance.list_gateway_views(
        control,
        settings=Settings(
            resources={
                "gateway_id": "managed",
                "gateway_url": launchpad["gatewayUrl"],
                "oauth_provider_arn": "arn:oauth",
                "registry_id": "",
            }
        ),
        refresh=True,
    )
    by_id = {view["id"]: view for view in views}
    assert by_id["managed"]["attachability"]["attachable"] is True
    assert by_id["managed"]["attachability"]["auth_type"] == "oauth"
    assert by_id["managed"]["policy_test_available"] is True
    assert by_id["external"]["attachability"] == {
        "attachable": False,
        "reason": "custom_jwt_provider_unmanaged",
        "auth_type": "oauth",
    }
    assert by_id["external"]["policy_test_available"] is False


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
            settings=Settings(resources=resources),
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
            "oauth_provider_arn": "arn:oauth",
            "registry_id": "",
        }
    )

    command = governance.gateway_detail(
        control,
        MagicMock(),
        "external",
        settings=settings,
    )["external_tools_list_command"]
    assert "tools/list" in command
    assert "https://external.example.test/mcp" in command
    # the template must reference an operator-side variable, never a real token
    assert "$GATEWAY_ACCESS_TOKEN" in command

    for gateway_id in ("managed", "iam"):
        assert (
            governance.gateway_detail(
                control,
                MagicMock(),
                gateway_id,
                settings=settings,
            )["external_tools_list_command"]
            is None
        )


def test_iam_preflight_pass_fail_and_unknown():
    iam = MagicMock()
    iam.simulate_principal_policy.return_value = {
        "EvaluationResults": [
            {"EvalActionName": action, "EvalDecision": "allowed"}
            for action in governance.POLICY_IAM_ACTIONS
        ]
    }
    passed = governance.iam_preflight(
        iam,
        role_arn="arn:role",
        engine_arn="arn:engine",
        gateway_arn="arn:gateway",
    )
    assert passed["status"] == "pass"

    iam.simulate_principal_policy.return_value["EvaluationResults"][0][
        "EvalDecision"
    ] = "implicitDeny"
    failed = governance.iam_preflight(
        iam,
        role_arn="arn:role",
        engine_arn="arn:engine",
        gateway_arn="arn:gateway",
    )
    assert failed["status"] == "fail"
    assert failed["missing_actions"] == [governance.POLICY_IAM_ACTIONS[0]]

    iam.simulate_principal_policy.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no"}},
        "SimulatePrincipalPolicy",
    )
    unknown = governance.iam_preflight(
        iam,
        role_arn="arn:role",
        engine_arn="arn:engine",
        gateway_arn="arn:gateway",
    )
    assert unknown["status"] == "unknown"
    assert unknown["operator_error"] == "AccessDenied"


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


def test_dangling_engine_reference_degrades_views_instead_of_raising():
    """A Gateway keeps its reference after the Engine is deleted out-of-band.

    Before this was handled, one dangling reference made the whole Gateway list
    answer 500 for every Gateway.
    """
    engine_arn = "arn:aws:bedrock-agentcore:us-west-2:123:policy-engine/pe-gone"
    control = _control([_gateway("gw-1", "alpha", engine_arn=engine_arn)])
    control.exceptions = SimpleNamespace(ResourceNotFoundException=_NotFound)
    control.get_policy_engine.side_effect = _NotFound("deleted")
    iam = MagicMock()
    iam.simulate_principal_policy.return_value = {
        "EvaluationResults": [
            {"EvalActionName": action, "EvalDecision": "allowed"}
            for action in governance.POLICY_IAM_ACTIONS
        ]
    }

    governance.invalidate_gateway_cache()
    [view] = governance.list_gateway_views(
        control,
        settings=Settings(resources={"registry_id": ""}),
        refresh=True,
    )
    assert view["policy_engine"] == {
        "id": "pe-gone",
        "arn": engine_arn,
        "name": None,
        "status": "DELETED",
        "status_reasons": [governance.DANGLING_ENGINE_REASON],
        "updated_at": None,
        "mode": "LOG_ONLY",
        "missing": True,
    }

    detail = governance.gateway_detail(
        control,
        iam,
        "gw-1",
        settings=Settings(resources={"registry_id": ""}),
    )
    assert detail["policy_engine"]["missing"] is True
