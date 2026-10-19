"""Agents use the COA context layer: the Harness per-turn override, the consent
short-circuit (sync + stream), /v1, the byte-identical path for every other agent,
the Strands deploy expansion, and the canary / evaluation refusals."""

import json
import time
from typing import Any

import pytest

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.deployer import harness as harness_deployer
from app.deployer.environment import runtime_environment
from app.deployer.return_url import register_return_url_stage
from app.models.ledger import Agent, ChatMessage, ContextLayer
from app.optimization.service import canary_capability, experiment_capability
from app.schemas.agent import AgentSpec
from app.services import agent_iam, context_layer, invoke, oauth_sessions
from app.services.chat import chat_stream
from app.services.workspace import context_for_workspace
from app.templates import gateway_support
from app.templates.identity_support import as_user_connections_stored, render_identity_source
from app.templates.strands_agent import render_main_py

from . import coa_fakes as fx

REF = {"namespace_id": fx.NAMESPACE, "namespace_name": "AnyCompany HR"}
HARNESS_ARN = f"arn:aws:bedrock-agentcore:{fx.WS_REGION}:{fx.ACCOUNT}:harness/hr_h-abc"
KB_GW_TOOL = {
    "type": "agentcore_gateway", "name": "launchpad_kb_gw",
    "config": {"agentCoreGateway": {"gatewayArn": "arn:kb-gw", "outboundAuth": {"oauth": {}}}},
}


@pytest.fixture
def account(monkeypatch):
    account = fx.install(monkeypatch)
    fx.connect_default(account)
    account.data.calls.clear()
    account.control.calls.clear()
    return account


def _agent(spec: dict, method: str = "harness") -> Agent:
    db = SessionLocal()
    try:
        row = Agent(workspace_id="default", name=spec["name"], method=method, status="active",
                    spec=spec, arn=HARNESS_ARN, resource_id="hr_h-abc", version="1")
        db.add(row)
        db.commit()
        return row
    finally:
        db.close()


def _harness(**fields) -> Agent:
    spec = {"name": "hr-h", "method": "harness", "system_prompt": "help", **fields}
    return _agent(spec)


def _invoked(account) -> dict[str, Any]:
    calls = account.data.ops("invoke_harness")
    assert len(calls) == 1
    return calls[0]


def _coa_tool(kwargs: dict[str, Any]) -> dict[str, Any]:
    return next(tool for tool in kwargs["tools"] if tool["name"] == "coa")


# ── Harness: the per-turn override ───────────────────────────────────────────


def test_harness_turn_carries_the_member_token_on_the_coa_tool(account):
    account.data.token = fx.jwt(time.time() + 3600)
    account.control.harness = {"tools": [KB_GW_TOOL], "allowedTools": ["@launchpad_kb_gw"]}
    agent = _harness(context_layer=REF)
    result = invoke.invoke_agent_text(
        agent, "EMP-3105 还能申请几天年假？", runtime_user_id="alice",
        workspace=context_for_workspace("default"),
    )
    assert result["text"] == "5 days"
    kwargs = _invoked(account)
    assert kwargs["runtimeUserId"] == "alice"
    # the deployed tools are kept verbatim; COA is added, never replaces
    assert kwargs["tools"][0] == KB_GW_TOOL
    mcp_url = (
        f"https://bedrock-agentcore.{fx.COA_REGION}.amazonaws.com/runtimes/"
        "arn%3Aaws%3Abedrock-agentcore%3Aus-east-2%3A111122223333%3Aruntime%2F"
        "coa_w2_dev_mcp_server-1cRyvG5BcK/invocations?qualifier=DEFAULT"
    )
    assert _coa_tool(kwargs) == {
        "type": "remote_mcp", "name": "coa",
        "config": {"remoteMcp": {
            "url": mcp_url,
            "headers": {"Authorization": f"Bearer {account.data.token}"},
        }},
    }
    assert kwargs["allowedTools"] == ["@launchpad_kb_gw", "@coa"]
    # the token was fetched for the signed-in user on the console workload
    assert account.data.ops("get_workload_access_token_for_user_id")[0]["userId"] == "alice"


def test_harness_explicit_allowed_tools_still_get_the_coa_selector(account):
    account.data.token = fx.jwt(time.time() + 3600)
    account.control.harness = {"tools": [], "allowedTools": []}
    agent = _harness(context_layer=REF, allowed_tools=[])
    invoke.invoke_agent_text(agent, "q", runtime_user_id="alice")
    assert _invoked(account)["allowedTools"] == ["@coa"]


def test_harness_with_a_gateway_token_swaps_the_gateway_and_adds_coa(account, monkeypatch):
    account.data.token = fx.jwt(time.time() + 3600)
    resources = {
        "gateway_arn": "arn:lp-gw", "gateway_url": "https://lp-gw/mcp",
        "gateway_id": "lp-gw", "oauth_provider_arn": "arn:m2m",
    }
    from app.models.ledger import Workspace

    db = SessionLocal()
    try:
        row = db.get(Workspace, "default")
        row.resources = {**(row.resources or {}), **resources}
        db.commit()
    finally:
        db.close()
    account.control.harness = {
        "tools": [{"type": "agentcore_gateway", "name": "launchpad_gw",
                   "config": {"agentCoreGateway": {"gatewayArn": "arn:lp-gw"}}}],
        "allowedTools": ["@launchpad_gw/office___*"],
    }
    agent = _harness(context_layer=REF, tools=[{"type": "gateway", "name": "office"}])
    invoke.invoke_agent_text(agent, "q", runtime_user_id="alice", gateway_access_token="pool-jwt")
    kwargs = _invoked(account)
    assert [tool["name"] for tool in kwargs["tools"]] == ["launchpad_gw_user", "coa"]
    assert kwargs["tools"][0]["config"]["remoteMcp"]["headers"] == {
        "Authorization": "Bearer pool-jwt"
    }
    assert kwargs["allowedTools"] == [
        "@launchpad_gw_user/launchpad_gw_user_office___*", "@coa",
    ]


def test_a_deployed_tool_named_coa_is_refused(account):
    account.data.token = fx.jwt(time.time() + 3600)
    account.control.harness = {"tools": [{"type": "remote_mcp", "name": "coa"}]}
    with pytest.raises(ValueError, match="already named 'coa'"):
        invoke.invoke_agent_text(_harness(context_layer=REF), "q", runtime_user_id="alice")
    assert account.data.ops("invoke_harness") == []


# ── consent short-circuit ────────────────────────────────────────────────────


def test_sync_turn_without_consent_returns_the_ask_and_never_invokes(account):
    agent = _harness(context_layer=REF)
    result = invoke.invoke_agent_text(agent, "q", session_id="s" * 40, runtime_user_id="alice")
    assert result["text"] == ""
    assert result["session_id"] == "s" * 40
    assert result["auth_required"] == [{
        "provider": "coa-coa-w2", "tool": "context_layer",
        "url": "https://coa-auth.example/authorize?state=x",
        "scopes": ["openid", "email", "profile"], "agent_id": agent.id,
    }]
    assert account.data.ops("invoke_harness") == []
    assert account.control.ops("get_harness") == []


def test_stream_turn_without_consent_yields_the_card_and_ends(account):
    agent = _harness(context_layer=REF)
    events = list(chat_stream(agent, "q", session_id="s" * 40, actor_id="hr__alice",
                              runtime_user_id="alice"))
    assert [e["event"] for e in events] == ["meta", "auth_required", "done"]
    assert events[1]["data"]["tool"] == "context_layer"
    assert "session_uri" not in events[1]["data"]
    assert account.data.ops("invoke_harness") == []


def test_stream_turn_with_consent_streams_the_answer(account):
    account.data.token = fx.jwt(time.time() + 3600)
    agent = _harness(context_layer=REF)
    events = list(chat_stream(agent, "q", actor_id="hr__alice", runtime_user_id="alice"))
    assert [e["event"] for e in events] == ["meta", "delta", "done"]
    assert _coa_tool(_invoked(account))["config"]["remoteMcp"]["headers"]["Authorization"] == (
        f"Bearer {account.data.token}"
    )


def test_v1_is_refused_by_name_on_both_entrances(account):
    account.data.token = fx.jwt(time.time() + 3600)  # even a vaulted token is not used
    agent = _harness(context_layer=REF)
    with pytest.raises(AppError) as exc:
        invoke.invoke_agent_text(agent, "q", console_user=False)
    assert exc.value.code == oauth_sessions.AS_USER_REQUIRES_CONSOLE
    events = list(chat_stream(agent, "q", console_user=False))
    assert events[-1]["event"] == "error"
    assert events[-1]["data"]["code"] == oauth_sessions.AS_USER_REQUIRES_CONSOLE
    assert account.data.calls == []


def test_a_caller_naming_no_user_fails_closed(account):
    with pytest.raises(AppError) as exc:
        invoke.invoke_agent_text(_harness(context_layer=REF), "q")
    assert exc.value.code == oauth_sessions.AS_USER_REQUIRES_CONSOLE


def test_a_layer_deleted_out_of_band_fails_closed(account):
    agent = _harness(context_layer=REF)
    db = SessionLocal()
    try:
        db.query(ContextLayer).delete()
        db.commit()
    finally:
        db.close()
    with pytest.raises(AppError) as exc:
        invoke.invoke_agent_text(agent, "q", runtime_user_id="alice")
    assert exc.value.code == "context_layer.not_connected"


# ── every other agent is unchanged ───────────────────────────────────────────


def test_an_agent_without_a_layer_sends_the_exact_same_request(account):
    agent = _harness(name="plain")
    invoke.invoke_agent_text(agent, "hi", session_id="s" * 40, actor_id="plain__bob",
                             runtime_user_id="bob")
    list(chat_stream(agent, "hi", session_id="t" * 40, actor_id="plain__bob"))
    sync, stream = account.data.ops("invoke_harness")
    assert sync == {
        "harnessArn": HARNESS_ARN, "runtimeSessionId": "s" * 40, "actorId": "plain__bob",
        "messages": [{"role": "user", "content": [{"text": "hi"}]}], "runtimeUserId": "bob",
    }
    assert stream == {
        "harnessArn": HARNESS_ARN, "runtimeSessionId": "t" * 40, "actorId": "plain__bob",
        "messages": [{"role": "user", "content": [{"text": "hi"}]}],
    }
    # no GetHarness, no vault call
    assert account.control.calls == []
    assert account.data.ops("get_resource_oauth2_token") == []


# ── Harness deploy: the prompt addendum, no COA tool ─────────────────────────


def test_harness_deploy_adds_the_addendum_and_no_coa_tool():
    spec = AgentSpec(name="hr-h", method="harness", system_prompt="help", context_layer=REF)
    params = harness_deployer.build_create_params(spec, "arn:role", None)
    prompt = params["systemPrompt"][0]["text"]
    assert prompt.startswith("help\n\n## Context layer (COA)")
    assert fx.NAMESPACE in prompt and "`coa_list_metrics`" in prompt and "AnyCompany HR" in prompt
    assert "tools" not in params
    assert params["allowedTools"] == []
    assert harness_deployer.strip_generated_prompt(spec, prompt) == "help"
    plain = AgentSpec(name="hr-h", method="harness", system_prompt="help")
    assert harness_deployer.build_create_params(plain, "arn:role", None)["systemPrompt"] == [
        {"text": "help"}
    ]


# ── Strands: one expansion feeds every deploy consumer ───────────────────────


def _zip_spec() -> dict:
    return {"name": "hr-z", "method": "zip_runtime", "system_prompt": "help",
            "context_layer": REF}


def test_strands_expansion_feeds_template_iam_env_and_return_url(account):
    layer = context_layer.require_connected("default")
    spec = context_layer.deploy_spec(_zip_spec(), "default")
    coa = next(tool for tool in spec.tools if tool.name == "coa")
    assert coa.type == "mcp" and coa.config == {"url": layer.mcp_url}
    assert (coa.auth.connection, coa.auth.mode, coa.auth.kind) == (
        "coa-coa-w2", "as_user", "oauth2",
    )
    assert "`list_metrics`" in spec.system_prompt and "`coa_list_metrics`" not in spec.system_prompt
    # template: the identity client is rendered with the MCP URL + Connection
    source = render_identity_source(spec)
    assert layer.mcp_url in source and "'coa-coa-w2'" in source
    assert layer.mcp_url in render_main_py(spec)
    # IAM: the per-Connection token grant
    ctx = agent_iam.role_context(context_for_workspace("default"))
    statements = {s["Sid"]: s for s in agent_iam.policy_document(spec, ctx)["Statement"]}
    assert any(
        r.endswith("oauth2credentialprovider/coa-coa-w2")
        for r in statements["ToolAuthOauth2Token"]["Resource"]
    )
    # environment: the as_user return URL + the outbound-auth declaration
    env = runtime_environment(spec, {}, agent_id="agent-1")
    assert env["LAUNCHPAD_OAUTH_RETURN_URL"].endswith("/auth/return")
    assert json.loads(env["LAUNCHPAD_OUTBOUND_AUTH"])[0]["connection"] == "coa-coa-w2"
    # return-URL allow-list on the runtime's workload identity
    account.control.workloads["hr_z_ab12cd-Xy12345678"] = []
    assert register_return_url_stage(account.control, spec, "hr_z_ab12cd-Xy12345678",
                                     lambda _m: None) is True
    assert account.control.workloads["hr_z_ab12cd-Xy12345678"]


def test_the_stored_spec_keeps_only_the_ref():
    stored = AgentSpec(**_zip_spec()).model_dump()
    assert stored["tools"] == []
    assert stored["context_layer"] == REF


def test_strands_hot_path_names_the_user_and_honours_revocation(account):
    stored = _zip_spec()
    assert gateway_support.runtime_user_id(stored, "alice") == "alice"
    assert gateway_support.runtime_user_id({**stored, "method": "harness"}, "alice") is None
    assert as_user_connections_stored(stored) == []
    assert as_user_connections_stored(stored, "coa-coa-w2") == ["coa-coa-w2"]
    agent = _agent(stored, method="zip_runtime")
    assert invoke._pending_force_reauth(agent, "alice") == []
    db = SessionLocal()
    try:
        oauth_sessions.revoke(db, "default", user_id="alice", provider="coa-coa-w2")
    finally:
        db.close()
    assert invoke._pending_force_reauth(agent, "alice") == ["coa-coa-w2"]


def test_a_strands_deploy_without_a_layer_fails_by_name(monkeypatch):
    fx.install(monkeypatch)
    with pytest.raises(AppError) as exc:
        context_layer.deploy_spec(_zip_spec(), "default")
    assert exc.value.code == "context_layer.not_connected"


# ── canary / experiments / evaluation / conversion refuse COA agents ─────────


def test_canary_and_experiments_refuse_a_layered_agent(account):
    for agent in (_harness(context_layer=REF), _agent(_zip_spec(), method="zip_runtime")):
        assert canary_capability(agent)["reason_code"] == "context-layer"
        assert canary_capability(agent)["eligible"] is False
        assert experiment_capability(agent)["reason_code"] == "context-layer"


def test_evaluation_replays_are_refused(account):
    from app.evaluation import service as eval_service

    with pytest.raises(AppError) as exc:
        eval_service.submit_run(
            agent=_harness(context_layer=REF), workspace=context_for_workspace("default"),
            dataset_items=[{"scenario_id": "1", "turns": [{"input": "q"}]}],
            dataset_id=None, dataset_name="d", evaluators=["Builtin.Helpfulness"],
        )
    assert exc.value.code == "context_layer.unsupported_method"


def test_conversion_is_refused(client, account):
    agent = _harness(context_layer=REF)
    response = client.post(f"/api/agents/{agent.id}/convert")
    assert response.status_code == 422
    assert response.json()["code"] == "context_layer.unsupported_method"


# ── console: the auth card, the chat route, the console invoke route ─────────


def test_the_chat_card_polls_the_console_grant_for_a_harness_agent(client, account):
    agent = _harness(context_layer=REF)
    invoke.invoke_agent_text(agent, "q", runtime_user_id="admin")
    status = client.get(
        f"/api/identity/grants/coa-coa-w2/status?agent_id={agent.id}"
    ).json()
    assert status["status"] == "pending" and status["agent_id"] == agent.id


def test_chat_route_names_the_signed_in_user_and_persists_the_card(client, account):
    agent = _harness(context_layer=REF)
    response = client.post(f"/api/chat/{agent.id}", json={"prompt": "q"})
    assert response.status_code == 200
    assert "event: auth_required" in response.text
    assert account.data.ops("get_workload_access_token_for_user_id")[0]["userId"] == "admin"
    db = SessionLocal()
    try:
        rows = db.query(ChatMessage).filter(ChatMessage.agent_id == agent.id).all()
    finally:
        db.close()
    assert [(r.role, r.text, r.name) for r in rows if r.role == "auth"] == [
        ("auth", "coa-coa-w2", "context_layer")
    ]

    account.data.token = fx.jwt(time.time() + 3600)
    answered = client.post(f"/api/chat/{agent.id}", json={"prompt": "q"})
    assert "5 days" in answered.text
    assert _invoked(account)["runtimeUserId"] == "admin"
    assert account.data.token not in answered.text


def test_console_invoke_route_acts_as_the_signed_in_user(client, account):
    account.data.token = fx.jwt(time.time() + 3600)
    agent = _harness(context_layer=REF)
    response = client.post(f"/api/agents/{agent.id}/invoke",
                           json={"prompt": "q", "actor_id": "someone-else"})
    assert response.status_code == 200, response.text
    assert account.data.ops("get_workload_access_token_for_user_id")[0]["userId"] == "admin"
    assert account.data.token not in response.text


def test_console_invoke_route_names_a_missing_consent_instead_of_answering_empty(
    client, account,
):
    agent = _harness(context_layer=REF)
    response = client.post(f"/api/agents/{agent.id}/invoke", json={"prompt": "q"})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "context_layer.authorization_required"
    assert body["detail"]["authorization_url"].startswith("https://")
    assert body["detail"]["connection"] == "coa-coa-w2"
    assert "urn:session" not in response.text  # the session uri stays server-side
    assert account.data.ops("invoke_harness") == []
