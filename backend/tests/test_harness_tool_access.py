"""Selection must reach the wire without implicit native tools or widened selectors."""

from copy import deepcopy
from unittest.mock import MagicMock

import botocore.session
import pytest
from botocore.validate import ParamValidator

from app.deployer.harness import build_create_params
from app.harness_tool_access import (
    NATIVE_HARNESS_TOOLS,
    remap_tool_patterns,
    selected_native_tools,
    selected_tool_patterns,
)
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import chat, invoke
from app.services.agentcore import harness as hc
from tests.conftest import ws_ctx

ROLE = "arn:aws:iam::111122223333:role/agent"
ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/agent-123"
GW = "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/shared-123"
KB = "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/kb-123"
RESOURCES = {
    "gateway_arn": GW, "gateway_id": "shared-123", "gateway_url": "https://gw.example/mcp",
    "kb_gateway_arn": KB, "oauth_provider_arn": "arn:oauth-provider",
}


def spec(**overrides):
    return AgentSpec(**{
        "name": "selected-agent", "method": "harness", "system_prompt": "Help the user.",
        **overrides,
    })


@pytest.mark.parametrize("native", [[], ["shell"], ["file_operations"], list(NATIVE_HARNESS_TOOLS)])
def test_native_selection_is_bounded_and_only_an_allowed_tools_setting(native):
    selected = spec(native_tools=native)
    params = build_create_params(selected, ROLE, None)
    assert params["allowedTools"] == native
    assert "tools" not in params
    assert AgentSpec.model_validate(selected.model_dump()).native_tools == native


@pytest.mark.parametrize("native", [
    ["unknown"], ["shell", "shell"], ["shell", "file_operations", "shell"], [None], "shell",
])
def test_invalid_native_selection_is_refused(native):
    with pytest.raises(ValueError):
        spec(native_tools=native)


@pytest.mark.parametrize("method", ["zip_runtime", "container", "studio"])
def test_non_harness_methods_only_accept_empty_native_selection(method):
    assert spec(method=method).native_tools == []
    with pytest.raises(ValueError, match="harness method only"):
        spec(method=method, native_tools=["shell"])


@pytest.mark.parametrize(("overrides", "expected"), [
    ({}, set()),
    ({"native_tools": ["shell"]}, {"shell"}),
    ({"native_tools": ["file_operations"]}, {"file_operations"}),
    ({"native_tools": ["shell"], "allowed_tools": []}, set()),
    ({"native_tools": ["shell"], "allowed_tools": ["file_*"]}, {"file_operations"}),
    ({"allowed_tools": ["*"]}, set(NATIVE_HARNESS_TOOLS)),
    ({"allowed_tools": ["shell"]}, {"shell"}),
    ({"allowed_tools": ["@builtin"]}, set(NATIVE_HARNESS_TOOLS)),
    ({"allowed_tools": ["@builtin/shell"]}, {"shell"}),
    ({"allowed_tools": ["@builtin/file_*"]}, {"file_operations"}),
    ({"allowed_tools": ["@other/shell", "@docs", "skills"]}, set()),
])
def test_evaluator_native_projection_follows_effective_override(overrides, expected):
    assert selected_native_tools(overrides) == expected


def test_selected_groups_use_final_gateway_names_and_do_not_enable_native_support():
    selected = spec(
        tools=[
            {"type": "builtin", "name": "browser"},
            {"type": "mcp", "name": "docs", "config": {"url": "https://docs.example/mcp"}},
            {"type": "mcp", "name": "missing-url"},
            {"type": "gateway", "name": "user-facing-label"},
        ],
        skills=["s3://skills/my-skill/"],
        knowledge_bases=[{"kb_id": "KB123", "name": "manual"}],
    )
    params = build_create_params(selected, ROLE, None, gateway_attachments=[
        {"gateway_arn": GW, "gateway_name": "real.gateway", "outbound_auth": {"none": {}}},
        {"gateway_arn": KB, "gateway_name": "real-gateway", "outbound_auth": {"none": {}}},
    ], kb_gateway={"arn": KB, "oauth_provider_arn": "arn:oauth"})
    assert params["allowedTools"] == [
        "@browser", "@docs", "@real_gateway", "@real_gateway_2", "skills",
    ]
    assert len(params["tools"]) == 4
    assert "user-facing-label" not in str(params["allowedTools"])
    assert not selected_native_tools({"allowed_tools": params["allowedTools"]})


@pytest.mark.parametrize("explicit", [[], ["*"], ["@docs/search", "file_*"]])
def test_explicit_overrides_survive_native_selection_and_keep_mounted_kb_support(explicit):
    selected = spec(
        allowed_tools=explicit, native_tools=["shell"], skills=["/skills"],
        knowledge_bases=[{"kb_id": "KB123", "name": "manual"}],
    )
    original = deepcopy(selected)
    params = build_create_params(
        selected, ROLE, None, kb_gateway={"arn": KB, "oauth_provider_arn": "arn:oauth"},
    )
    assert params["allowedTools"] == [*explicit, "@launchpad_kb_gw"]
    assert selected == original
    assert build_create_params(spec(allowed_tools=explicit), ROLE, None)["allowedTools"] == explicit


def test_last_deselection_clears_update_and_empty_is_sdk_valid():
    params = build_create_params(spec(), ROLE, None)
    update = hc.wrap_params_for_update({**params, "harnessId": "selected-agent-123"})
    assert update["tools"] == update["skills"] == update["allowedTools"] == []
    session = botocore.session.get_session()
    for operation, payload in [("CreateHarness", params), ("UpdateHarness", update)]:
        shape = session.get_service_model("bedrock-agentcore-control").operation_model(
            operation
        ).input_shape
        errors = ParamValidator().validate(payload, shape)
        assert not errors.has_errors(), errors.generate_report()


def test_shared_selection_preserves_order_and_deduplicates_groups():
    assert selected_tool_patterns(
        [{"name": "docs"}, {"name": "docs"}, {"name": "other"}], True, ["shell"],
    ) == ["@docs", "@other", "skills", "shell"]


@pytest.mark.parametrize("name", [
    "builtin", "BUILTIN", "*", "built?n", "[b]uiltin", "@builtin", "builtin/shell",
    "builtin\n", "builtin\x00", " builtin", "docs tools", "docs\\*", "docs;builtin",
    "", None, "x" * 64,
])
def test_derived_group_names_cannot_admit_native_tools_or_selector_syntax(name):
    with pytest.raises(ValueError, match="cannot derive allowedTools"):
        selected_tool_patterns([{"name": name}], has_skills=False, native_tools=[])


@pytest.mark.parametrize("name", ["builtin", "*", "built?n", "[b]uiltin", "builtin/shell"])
def test_remote_mcp_cannot_broaden_zero_native_selection_through_its_name(name):
    selected = spec(
        tools=[{"type": "mcp", "name": name, "config": {"url": "https://docs.example/mcp"}}],
        native_tools=[],
    )
    with pytest.raises(ValueError, match="cannot derive allowedTools"):
        build_create_params(selected, ROLE, None)


def test_resolved_gateway_reserved_alias_is_refused_without_guessing_a_new_name():
    with pytest.raises(ValueError, match="'builtin' is reserved"):
        build_create_params(spec(), ROLE, None, gateway_attachments=[
            {"gateway_arn": GW, "gateway_name": "builtin", "outbound_auth": {"none": {}}},
        ])


@pytest.mark.parametrize("explicit", [[], ["*"], ["@builtin"], ["file_*"]])
def test_reserved_config_name_does_not_rewrite_explicit_expert_override(explicit):
    selected = spec(
        tools=[{"type": "mcp", "name": "builtin", "config": {"url": "https://docs.example/mcp"}}],
        allowed_tools=explicit,
    )
    params = build_create_params(selected, ROLE, None)
    assert params["allowedTools"] == explicit
    assert params["tools"][0]["name"] == "builtin"


def test_safe_group_names_fit_wire_bound_and_do_not_select_natives():
    allowed = selected_tool_patterns(
        [{"name": name} for name in ["aws-knowledge", "launchpad_kb_gw", "x" * 63]],
        has_skills=True, native_tools=[],
    )
    assert allowed == ["@aws-knowledge", "@launchpad_kb_gw", "@" + "x" * 63, "skills"]
    assert max(map(len, allowed)) == 64
    assert selected_native_tools({"allowed_tools": allowed}) == set()


def test_group_remapping_retains_narrow_suffix_without_wildcard_widening():
    names = {"deployed_alias": "launchpad_gw_user", "deployed_docs": "deployed_docs"}
    assert remap_tool_patterns(
        ["@deployed_alias/hr___lookup", "@deployed_*/read_*", "deployed_alias/lookup",
         "@untouched", "file_*", "*"],
        names,
    ) == [
        "@launchpad_gw_user/hr___lookup",
        "@launchpad_gw_user/read_*", "@deployed_docs/read_*",
        "launchpad_gw_user/lookup", "@untouched", "file_*", "*",
    ]


def data_client():
    data = MagicMock()
    data.invoke_harness.return_value = {"stream": iter([
        {"contentBlockDelta": {"delta": {"text": "answer"}}},
        {"messageStop": {"stopReason": "end_turn"}},
    ])}
    return data


@pytest.mark.parametrize("entrance", ["sync", "sse"])
@pytest.mark.parametrize("native", [[], ["shell"]])
def test_authenticated_request_derives_from_actual_override_names(monkeypatch, entrance, native):
    selected = spec(
        native_tools=native, skills=["/skills"],
        tools=[
            {"type": "gateway", "name": "display-label", "config": {"gateway_id": "shared-123"}},
            {"type": "gateway", "name": "other-label", "config": {"gateway_id": "other-123"}},
            {"type": "mcp", "name": "docs", "config": {"url": "https://docs.example/mcp"}},
        ],
        knowledge_bases=[{"kb_id": "KB123", "name": "manual"}],
    )
    agent = Agent(method="harness", status="active", name=selected.name, arn=ARN,
                  resource_id="agent-123", spec=selected.model_dump())
    data = data_client()
    monkeypatch.setattr(chat, "data_client", lambda _: data)
    monkeypatch.setattr(invoke, "data_client", lambda _: data)
    configured = [
        {"name": "actual_shared", "type": "agentcore_gateway",
         "config": {"agentCoreGateway": {"gatewayArn": GW, "outboundAuth": {"none": {}}}}},
        {"name": "other_gateway", "type": "agentcore_gateway",
         "config": {"agentCoreGateway": {"gatewayArn": "arn:other", "outboundAuth": {"none": {}}}}},
        {"name": "docs", "type": "remote_mcp",
         "config": {"remoteMcp": {"url": "https://docs.example/mcp"}}},
        {"name": "launchpad_kb_gw", "type": "agentcore_gateway",
         "config": {"agentCoreGateway": {"gatewayArn": KB, "outboundAuth": {"none": {}}}}},
    ]
    control = MagicMock()
    control.get_harness.return_value = {"harness": {"tools": configured}}
    monkeypatch.setattr(invoke, "control_client", lambda _: control)
    kwargs = {
        "workspace": ws_ctx(RESOURCES), "gateway_access_token": "trusted-token",
        "runtime_user_id": "operator", "actor_id": "agent__operator",
    }
    if entrance == "sync":
        assert invoke.invoke_agent_text(agent, "hello", **kwargs)["text"] == "answer"
    else:
        assert list(chat.chat_stream(agent, "hello", **kwargs))[-1]["event"] == "done"
    request = data.invoke_harness.call_args.kwargs
    assert request["allowedTools"] == [
        "@launchpad_gw_user", "@other_gateway", "@docs", "@launchpad_kb_gw", "skills", *native,
    ]
    assert request["tools"][1:] == configured[1:]
    control.get_harness.assert_called_once_with(harnessId="agent-123")
    assert request["runtimeUserId"] == "operator"
    assert request["tools"][0]["config"]["remoteMcp"]["headers"] == {
        "Authorization": "Bearer trusted-token",
    }
    assert all(t["name"] not in NATIVE_HARNESS_TOOLS for t in request["tools"])


@pytest.mark.parametrize("entrance", ["sync", "sse"])
def test_explicit_user_gateway_selector_uses_deployed_alias_and_preserves_other_configs(
    monkeypatch, entrance,
):
    selected = spec(
        native_tools=["shell"],
        allowed_tools=["@actual_alias/hr___lookup", "@other_gateway/read_*", "file_*"],
        tools=[{"type": "gateway", "name": "wrong-alias"}],
        knowledge_bases=[{"kb_id": "KB123", "name": "manual"}],
    )
    agent = Agent(method="harness", status="active", name=selected.name, arn=ARN,
                  resource_id="agent-123",
                  spec=selected.model_dump())
    configured = [
        {"name": "actual_alias", "type": "agentcore_gateway",
         "config": {"agentCoreGateway": {"gatewayArn": GW, "outboundAuth": {"none": {}}}}},
        {"name": "other_gateway", "type": "agentcore_gateway",
         "config": {"agentCoreGateway": {"gatewayArn": "arn:other", "outboundAuth": {"none": {}}}}},
        {"name": "real_kb_alias", "type": "agentcore_gateway",
         "config": {"agentCoreGateway": {"gatewayArn": KB, "outboundAuth": {"none": {}}}}},
    ]
    before = deepcopy(configured)
    control = MagicMock()
    control.get_harness.return_value = {"harness": {"tools": configured}}
    monkeypatch.setattr(invoke, "control_client", lambda _: control)
    data = data_client()
    monkeypatch.setattr(chat, "data_client", lambda _: data)
    monkeypatch.setattr(invoke, "data_client", lambda _: data)
    kwargs = {"workspace": ws_ctx(RESOURCES), "gateway_access_token": "trusted-token"}
    if entrance == "sync":
        invoke.invoke_agent_text(agent, "hello", **kwargs)
    else:
        assert list(chat.chat_stream(agent, "hello", **kwargs))[-1]["event"] == "done"
    control.get_harness.assert_called_once_with(harnessId="agent-123")
    request = data.invoke_harness.call_args.kwargs
    assert request["allowedTools"] == [
        "@launchpad_gw_user/hr___lookup", "@other_gateway/read_*", "file_*", "@real_kb_alias",
    ]
    assert request["tools"][1:] == configured[1:]
    assert configured == before
    assert agent.spec["allowed_tools"] == selected.allowed_tools


@pytest.mark.parametrize("explicit", [[], ["*"], ["shell"]])
def test_authenticated_override_keeps_explicit_native_patterns_and_empty(monkeypatch, explicit):
    agent = Agent(method="harness", status="active", name="plain-agent", arn=ARN,
                  spec=spec(allowed_tools=explicit, native_tools=["file_operations"]).model_dump())
    data = data_client()
    monkeypatch.setattr(invoke, "data_client", lambda _: data)
    invoke.invoke_agent_text(agent, "hello", workspace=ws_ctx(),
                             gateway_access_token="trusted-token")
    request = data.invoke_harness.call_args.kwargs
    assert request["tools"] == []
    assert request["allowedTools"] == explicit


def test_authenticated_alias_collision_is_refused_before_invocation(monkeypatch):
    selected = spec(tools=[
        {"type": "gateway", "name": "shared", "config": {"gateway_id": "shared-123"}},
        {"type": "mcp", "name": "launchpad_gw_user", "config": {"url": "https://other.example/mcp"}},
    ])
    agent = Agent(method="harness", status="active", name=selected.name, arn=ARN,
                  resource_id="agent-123", spec=selected.model_dump())
    control = MagicMock()
    control.get_harness.return_value = {"harness": {"tools": [
        {"name": "actual_alias", "type": "agentcore_gateway",
         "config": {"agentCoreGateway": {"gatewayArn": GW}}},
        {"name": "launchpad_gw_user", "type": "remote_mcp"},
    ]}}
    monkeypatch.setattr(invoke, "control_client", lambda _: control)
    data = data_client()
    monkeypatch.setattr(invoke, "data_client", lambda _: data)
    with pytest.raises(ValueError, match="alias conflicts"):
        invoke.invoke_agent_text(agent, "hello", workspace=ws_ctx(RESOURCES),
                                 gateway_access_token="trusted-token")
    data.invoke_harness.assert_not_called()


def test_deployed_alias_lookup_failure_never_falls_back_to_wider_access(monkeypatch):
    selected = spec(allowed_tools=["@actual_alias/lookup"])
    agent = Agent(method="harness", status="active", name=selected.name, arn=ARN,
                  resource_id="agent-123", spec=selected.model_dump())
    control = MagicMock()
    control.get_harness.side_effect = RuntimeError("lookup failed")
    monkeypatch.setattr(invoke, "control_client", lambda _: control)
    data = data_client()
    monkeypatch.setattr(invoke, "data_client", lambda _: data)
    with pytest.raises(RuntimeError, match="lookup failed"):
        invoke.invoke_agent_text(agent, "hello", workspace=ws_ctx(RESOURCES),
                                 gateway_access_token="trusted-token")
    data.invoke_harness.assert_not_called()


@pytest.mark.parametrize("entrance", ["sync", "sse"])
def test_m2m_entrances_inherit_deployed_allowed_tools_without_recreating_configs(
    monkeypatch, entrance,
):
    agent = Agent(method="harness", status="active", name="plain-agent", arn=ARN,
                  spec=spec(native_tools=[]).model_dump())
    data = data_client()
    monkeypatch.setattr(chat, "data_client", lambda _: data)
    monkeypatch.setattr(invoke, "data_client", lambda _: data)
    if entrance == "sync":
        invoke.invoke_agent_text(agent, "hello", workspace=ws_ctx())
    else:
        assert list(chat.chat_stream(agent, "hello", workspace=ws_ctx()))[-1]["event"] == "done"
    request = data.invoke_harness.call_args.kwargs
    assert "tools" not in request and "allowedTools" not in request
