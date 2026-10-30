"""Gateway identity, endpoint and invocation grant agree across multi-gateway runtimes."""

import json
from types import SimpleNamespace

from app.deployer.environment import runtime_environment
from app.schemas.agent import AgentSpec, ToolRef
from app.services import method_validation, policy_engines


def test_home_gateway_url_is_replaced_before_runtime_rendering():
    workspace = SimpleNamespace(
        resources={"gateway_id": "gateway-home", "gateway_url": "https://home.example/mcp"})
    spec = AgentSpec(name="multi", method="zip_runtime", system_prompt="test", tools=[
        ToolRef(type="gateway", name="home", config={
            "gateway_id": "gateway-home", "gateway_url": "https://forged.example/mcp",
            "tools": ["home___echo"]})])
    method_validation._require_gateway_ids_resolved(spec, workspace)
    env = runtime_environment(spec, workspace.resources)
    assert env["LAUNCHPAD_GATEWAY_URL"] == "https://home.example/mcp"
    assert "forged" not in env["LAUNCHPAD_GATEWAYS"]


def test_second_gateway_is_resolved_and_each_keeps_its_tools(monkeypatch):
    from app.services.agentcore import client, gateway

    workspace = SimpleNamespace(account_id="111122223333", region="cn-northwest-1",
                                resources={"gateway_id": "gateway-home",
                                           "gateway_url": "https://home.example/mcp"})
    monkeypatch.setattr(client, "control_client", lambda _ws: object())
    monkeypatch.setattr(gateway, "get_gateway", lambda _client, gid: {
        "gatewayArn": f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:gateway/{gid}",
        "gatewayUrl": "https://second.example/mcp"})
    spec = AgentSpec(name="multi", method="zip_runtime", system_prompt="test", tools=[
        ToolRef(type="gateway", name=name, config={"gateway_id": f"gateway-{name}",
                                                 "tools": [f"{name}___echo"]})
        for name in ("home", "second")])
    method_validation._require_gateway_ids_resolved(spec, workspace)
    env = runtime_environment(spec, workspace.resources)
    groups = json.loads(env["LAUNCHPAD_GATEWAYS"])
    assert groups == [
        {"url": "https://home.example/mcp", "tools": ["home___echo"]},
        {"url": "https://second.example/mcp", "tools": ["second___echo"]}]


def test_gateway_invoke_grant_targets_the_gateway_role_and_local_function():
    calls = []
    iam = SimpleNamespace(put_role_policy=lambda **kw: calls.append(kw))
    context = SimpleNamespace(client=lambda service: iam if service == "iam" else None)
    facts = {"context": context, "role_arn": "arn:aws-cn:iam::111122223333:role/path/probe",
             "interceptor_function_arn":
                 "arn:aws-cn:lambda:cn-northwest-1:111122223333:function:interceptor"}
    policy_engines._grant_gateway_invoke(facts)
    policy_engines._grant_gateway_invoke(facts)
    assert calls[0] == calls[1] and calls[0]["RoleName"] == "probe"
    statement = json.loads(calls[0]["PolicyDocument"])["Statement"]
    assert statement == [{"Sid": "InvokePolicyInterceptor", "Effect": "Allow",
                          "Action": "lambda:InvokeFunction",
                          "Resource": facts["interceptor_function_arn"]}]
