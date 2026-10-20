"""Harness agents in gateway mode (``context_layer.via = "gateway"``, task
``10-20-gw-harness``): the stored shape of ``via``, the deploy (the managed COA
gateway as an attachment + the prompt, refused without it), the turn (the consent
gate, then the user-JWT swap, failing closed), the routes, and every direct agent
unchanged.

Hermetic: AWS is ``coa_fakes`` + the gateway control plane of
``test_context_layer_gateway``; the gateway's MCP endpoint is the consent tests'
``GatewayMcp`` behind an ``httpx.MockTransport``.
"""

import json
import time
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.deployer import harness as harness_deployer
from app.deployer import zip_runtime
from app.deployer.pipeline import StageContext
from app.models.ledger import Agent, ChatMessage, OauthPendingSession
from app.routers import agents as agents_router
from app.schemas.agent import AgentSpec
from app.services import (
    agent_iam,
    agent_source,
    context_layer,
    invoke,
    oauth_sessions,
    policy_identity,
    registry_console,
)
from app.services import context_layer_gateway as gw
from app.services.chat import chat_stream
from app.services.workspace import context_for_workspace
from app.templates import context_layer_support as support
from app.templates import gateway_support

from . import coa_fakes as fx
from .conftest import set_default_resources
from .test_context_layer_gateway import (
    CONSOLE_CLIENT,
    DISCOVERY,
    M2M_CLIENT,
    RESOURCES,
    GatewayControl,
)
from .test_context_layer_gateway_consent import (
    AUTH_URL,
    GATEWAY_ID,
    GATEWAY_URL,
    USER_JWT,
    GatewayMcp,
)
from .test_context_layer_gateway_consent import REF as GATEWAY_REF

CONNECTION = "coa-coa-w2"
NS = {"namespace_id": fx.NAMESPACE, "namespace_name": "AnyCompany HR"}
VIA_GATEWAY = {**NS, "via": "gateway"}
HARNESS_ARN = f"arn:aws:bedrock-agentcore:{fx.WS_REGION}:{fx.ACCOUNT}:harness/hr_gw-abc"
EMAIL = "ada@acme-corp.com"
# the managed gateway as AWS reads it back: trusting the workspace pool's members
TRUSTING = {
    "name": gw.GATEWAY_NAME,
    "protocolType": "MCP",
    "authorizerType": "CUSTOM_JWT",
    "authorizerConfiguration": {"customJWTAuthorizer": {
        "discoveryUrl": DISCOVERY, "allowedClients": [CONSOLE_CLIENT, M2M_CLIENT],
    }},
}
M2M_OUTBOUND = {"oauth": {
    "providerArn": RESOURCES["oauth_provider_arn"],
    "grantType": "CLIENT_CREDENTIALS",
    "scopes": ["launchpad-gw/invoke"],
}}
DEPLOYED_ATTACHMENT = {
    "type": "agentcore_gateway",
    "name": "launchpad_coa_gw",
    "config": {"agentCoreGateway": {
        "gatewayArn": GATEWAY_REF["gateway_arn"], "outboundAuth": M2M_OUTBOUND,
    }},
}
SWAPPED = {
    "type": "remote_mcp",
    "name": "launchpad_coa_gw",
    "config": {"remoteMcp": {
        "url": GATEWAY_URL, "headers": {"Authorization": f"Bearer {USER_JWT}"},
    }},
}


@pytest.fixture
def mcp(monkeypatch):
    server = GatewayMcp()
    monkeypatch.setattr(gw, "_transport", lambda: httpx.MockTransport(server.handler))
    monkeypatch.setattr(gw, "_sleep", lambda s: None)
    gw.forget_consents()
    yield server
    gw.forget_consents()


@pytest.fixture
def minted(monkeypatch):
    """``policy_identity.gateway_user_token`` stubbed: the member's pool JWT."""
    calls: list[tuple] = []

    def mint(workspace, username, role, email=None, *, min_valid_s=60):
        calls.append((username, role, email, min_valid_s))
        return USER_JWT

    monkeypatch.setattr(policy_identity, "gateway_user_token", mint)
    return calls


@pytest.fixture
def account(monkeypatch, client, mcp):
    """A connected layer, a bootstrapped workspace and an enabled managed gateway."""
    account = fx.Account()
    account.control = GatewayControl()
    fx.install(monkeypatch, account)
    fx.connect_default(account)
    set_default_resources(RESOURCES)
    _set_ref(GATEWAY_REF)
    account.control.add_gateway(GATEWAY_ID, **TRUSTING)
    account.control.calls.clear()
    account.data.calls.clear()
    return account


def _ws():
    return context_for_workspace("default")


def _set_ref(ref: dict | None) -> None:
    db = SessionLocal()
    try:
        gw._save_ref(db, "default", ref)
    finally:
        db.close()


def _spec(ref: dict | None = None, method: str = "harness", name: str = "hr-gw",
          **fields) -> AgentSpec:
    return AgentSpec(name=name, method=method, system_prompt="help",
                     context_layer=VIA_GATEWAY if ref is None else ref, **fields)


def _agent(ref: dict | None = None, method: str = "harness", name: str = "hr-gw",
           **fields) -> Agent:
    """A live agent whose row holds the spec exactly as a create stores it."""
    db = SessionLocal()
    try:
        row = Agent(workspace_id="default", name=name, method=method, status="active",
                    spec=_spec(ref, method, name, **fields).model_dump(), arn=HARNESS_ARN,
                    resource_id="hr_gw-abc", version="1")
        db.add(row)
        db.commit()
        db.refresh(row)
        db.expunge(row)
        return row
    finally:
        db.close()


def _deploy(account, agent: Agent) -> dict[str, Any]:
    """What the deploy would send, installed as the Harness AWS reads back."""
    params = harness_deployer._build_live_params(AgentSpec(**agent.spec), _ws())
    account.control.harness = {
        "tools": params.get("tools", []), "allowedTools": params["allowedTools"],
    }
    account.control.calls.clear()
    return params


def _invoked(account) -> dict[str, Any]:
    (call,) = account.data.ops("invoke_harness")
    return call


def _turn(agent: Agent, **kwargs):
    return invoke.invoke_agent_text(
        agent, "EMP-3105 还能申请几天年假？", runtime_user_id="ada", user_role="member",
        user_email=EMAIL, workspace=_ws(), **kwargs,
    )


def _vault_calls(account) -> list:
    return [op for op, _ in account.data.calls if op != "invoke_harness"]


# ── the stored shape ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("ref", [NS, {**NS, "via": "direct"}], ids=["omitted", "direct"])
def test_a_direct_ref_is_stored_without_via(ref):
    spec = _spec(ref)
    assert spec.context_layer.via == "direct"
    stored = spec.model_dump()
    # golden: the exact shape every direct spec had before ``via`` existed
    assert stored["context_layer"] == NS
    assert '"via"' not in spec.model_dump_json()
    assert AgentSpec(**stored).model_dump() == stored
    assert not support.uses_gateway(stored) and not support.via_gateway(spec)


def test_a_gateway_ref_is_stored_with_via():
    spec = _spec()
    stored = spec.model_dump()
    assert stored["context_layer"] == VIA_GATEWAY
    assert AgentSpec(**stored).context_layer.via == "gateway"
    assert support.uses_gateway(stored) and support.via_gateway(spec)
    assert support.harness_tool_prefix(spec) == "launchpad_coa_gw_coa___"
    assert support.harness_tool_prefix(_spec(NS)) == "coa_"


def test_via_is_direct_or_gateway():
    with pytest.raises(ValidationError):
        _spec({**NS, "via": "proxy"})


@pytest.mark.parametrize("fields", [{"method": "harness"}, {"method": "zip_runtime"}])
def test_gateway_mode_is_allowed_where_a_layer_is(fields):
    assert _spec(**fields).context_layer.via == "gateway"


def test_gateway_mode_is_refused_where_a_layer_is():
    with pytest.raises(ValidationError) as exc:
        _spec(method="container")
    assert "context_layer.unsupported_method" in {e["type"] for e in exc.value.errors()}


# ── deploy ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ref", [NS, {**NS, "via": "direct"}], ids=["omitted", "direct"])
def test_a_direct_harness_deploys_exactly_as_before(account, ref):
    """Golden: an enabled managed gateway changes nothing for a direct agent."""
    spec = _spec(ref)
    params = harness_deployer._build_live_params(spec, _ws())
    assert params == {
        "harnessName": "hr_gw",
        "executionRoleArn": "",
        "model": {"bedrockModelConfig": harness_deployer.model_config(spec)},
        "systemPrompt": [{"text": "help" + support.prompt_addendum(
            fx.NAMESPACE, "AnyCompany HR", tool_prefix="coa_",
        )}],
        "maxIterations": spec.max_iterations,
        "timeoutSeconds": spec.timeout_seconds,
        "allowedTools": [],
        "environment": {"agentCoreRuntimeEnvironment": {
            "filesystemConfigurations": [{"sessionStorage": {"mountPath": "/mnt/workspace"}}],
        }},
    }
    assert "`coa_list_metrics`" in params["systemPrompt"][0]["text"]
    _set_ref(None)
    assert harness_deployer._build_live_params(spec, _ws()) == params
    assert account.control.calls == []


def test_gateway_mode_attaches_the_managed_gateway_and_names_its_tools(account):
    spec = _spec()
    params = harness_deployer._build_live_params(spec, _ws())
    assert params["tools"] == [DEPLOYED_ATTACHMENT]
    assert params["allowedTools"] == ["@launchpad_coa_gw"]
    prompt = params["systemPrompt"][0]["text"]
    assert prompt == "help" + support.prompt_addendum(
        fx.NAMESPACE, "AnyCompany HR", tool_prefix="launchpad_coa_gw_coa___",
    )
    for tool in support.COA_TOOLS:
        assert f"`launchpad_coa_gw_coa___{tool}`" in prompt
    assert "`coa_list_metrics`" not in prompt
    assert account.control.calls == []  # ledger identifiers only, no AWS read


def test_the_attachment_is_what_the_registry_maps_for_the_live_gateway(account):
    """The deploy-time identity of a user-trusting gateway attached from the Registry
    (``registry_console._gateway_auth``), under the alias the prompt names."""
    live = registry_console._gateway_attachment(
        account.control.gateways[GATEWAY_ID], _ws().resources,
    )
    ours = gw.harness_attachment(context_layer.require_connected("default"), _ws().resources)
    assert ours == {**live, "gateway_name": "launchpad_coa_gw"}
    assert ours["outbound_auth"] == M2M_OUTBOUND and ours["attachable"] is True


def test_an_explicit_allowed_tools_list_keeps_the_gateway(account):
    params = harness_deployer._build_live_params(_spec(allowed_tools=[]), _ws())
    assert params["allowedTools"] == ["@launchpad_coa_gw"]


def test_the_managed_gateway_claims_its_alias_first(account, monkeypatch):
    other = {
        "gateway_id": "crm-gw-abc", "gateway_arn": "arn:crm-gw",
        "gateway_name": "launchpad-coa-gw",  # a clashing name never takes the alias
        "outbound_auth": {"awsIam": {}},
    }
    monkeypatch.setattr(registry_console, "resolve_gateway_attachments", lambda *a, **k: [other])
    params = harness_deployer._build_live_params(_spec(), _ws())
    assert [t["name"] for t in params["tools"]] == ["launchpad_coa_gw", "launchpad_coa_gw_2"]
    assert params["tools"][0] == DEPLOYED_ATTACHMENT


def test_a_registry_attachment_of_the_managed_gateway_is_not_deployed_twice(account, monkeypatch):
    live = registry_console._gateway_attachment(
        account.control.gateways[GATEWAY_ID], _ws().resources,
    )
    monkeypatch.setattr(registry_console, "resolve_gateway_attachments", lambda *a, **k: [live])
    params = harness_deployer._build_live_params(_spec(), _ws())
    assert params["tools"] == [DEPLOYED_ATTACHMENT]


def test_no_managed_gateway_is_refused_before_any_registry_read(account, monkeypatch):
    _set_ref(None)
    reads: list[tuple] = []
    monkeypatch.setattr(registry_console, "resolve_gateway_attachments",
                        lambda *a, **k: reads.append(a) or [])
    with pytest.raises(AppError) as exc:
        harness_deployer._build_live_params(_spec(), _ws())
    assert exc.value.code == "context_layer.gateway_unavailable"
    assert reads == []


def test_the_prompt_strips_either_addendum():
    spec = _spec()
    for prefix in ("launchpad_coa_gw_coa___", "coa_"):  # the mode may have changed since
        deployed = "help" + support.prompt_addendum(fx.NAMESPACE, "AnyCompany HR",
                                                    tool_prefix=prefix)
        assert harness_deployer.strip_generated_prompt(spec, deployed) == "help"
        assert harness_deployer.strip_generated_prompt(_spec(NS), deployed) == "help"


def _stage_ctx(agent: Agent) -> StageContext:
    return StageContext(agent_id=agent.id, deployment_id="dep-1", job_id="job-1",
                        workspace=_ws())


def test_a_gateway_mode_harness_without_the_gateway_fails_at_generate(account):
    agent = _agent()
    _set_ref(None)
    ctx = _stage_ctx(agent)
    with pytest.raises(AppError) as exc:
        harness_deployer._stage_generate(ctx, agent)
    assert exc.value.code == "context_layer.gateway_unavailable"
    assert "Context Layer page" in exc.value.message
    assert "create_params" not in ctx.scratch
    assert account.control.calls == []


def test_a_gateway_mode_harness_generates_with_the_gateway(account):
    agent = _agent()
    ctx = _stage_ctx(agent)
    harness_deployer._stage_generate(ctx, agent)
    assert ctx.scratch["create_params"]["tools"] == [DEPLOYED_ATTACHMENT]


def test_a_strands_agent_in_gateway_mode_is_refused_at_generate(account):
    agent = _agent(method="zip_runtime", name="hr-gw-z")
    with pytest.raises(AppError) as exc:
        zip_runtime._stage_generate(_stage_ctx(agent), agent)
    assert exc.value.code == "context_layer.gateway_unsupported_method"
    assert exc.value.status_code == 422
    assert "support follows" in exc.value.message


def test_the_execution_role_can_run_the_deployed_attachment():
    """As for a Registry-attached gateway: the workload-token grant, Harness only."""
    ctx = agent_iam.role_context(_ws())

    def sids(spec: AgentSpec) -> set[str]:
        return {s["Sid"] for s in agent_iam.policy_document(spec, ctx)["Statement"]}

    assert "AgentCoreWorkloadIdentity" in sids(_spec())
    assert "AgentCoreWorkloadIdentity" not in sids(_spec(NS))


def test_the_code_view_shows_the_gateway_tool(account):
    """R5: the Harness code view is GetHarness, allow-listed — the attachment shows."""
    params = harness_deployer._build_live_params(_spec(), _ws())
    projected = agent_source.project_harness(
        {"tools": params["tools"], "allowedTools": params["allowedTools"]}
    )
    assert projected["tools"] == [DEPLOYED_ATTACHMENT]
    assert projected["allowedTools"] == ["@launchpad_coa_gw"]


# ── request time ─────────────────────────────────────────────────────────────


@pytest.fixture
def launched(monkeypatch):
    jobs: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: jobs.append(jid))
    return jobs


def _body(ref: dict, name: str = "hr-gw") -> dict:
    return {"name": name, "method": "harness", "system_prompt": "help", "context_layer": ref}


def test_create_in_gateway_mode_needs_the_gateway(client, account, launched):
    _set_ref(None)
    response = client.post("/api/agents", json=_body(VIA_GATEWAY))
    assert response.status_code == 409
    assert response.json()["code"] == "context_layer.gateway_unavailable"
    assert launched == []


def test_create_stores_the_mode_and_the_gateway_lists_the_agent(client, account, launched):
    response = client.post("/api/agents", json=_body(VIA_GATEWAY))
    assert response.status_code == 202, response.text
    agent = response.json()["agent"]
    assert agent["spec"]["context_layer"] == VIA_GATEWAY
    direct = client.post("/api/agents", json=_body({**NS, "via": "direct"}, name="hr-direct"))
    assert direct.status_code == 202, direct.text
    assert direct.json()["agent"]["spec"]["context_layer"] == NS
    assert len(launched) == 2
    db = SessionLocal()
    try:
        # the raw-spec reads behind the gateway card and the disable guard
        assert [a["name"] for a in gw.gateway_agents(db, "default")] == ["hr-gw"]
        assert {a["name"] for a in context_layer.referencing_agents(db, "default")} == {
            "hr-gw", "hr-direct",
        }
    finally:
        db.close()


# ── the turn ─────────────────────────────────────────────────────────────────


def test_runtime_user_id_names_the_member_in_gateway_mode():
    assert gateway_support.runtime_user_id(_spec().model_dump(), "ada") == "ada"
    assert gateway_support.runtime_user_id(_spec(NS).model_dump(), "ada") is None
    zip_spec = _spec(method="zip_runtime").model_dump()
    assert gateway_support.runtime_user_id(zip_spec, "ada") == "ada"


def test_a_turn_without_consent_ends_at_the_card(account, minted, mcp):
    agent = _agent()
    _deploy(account, agent)
    result = _turn(agent, session_id="s" * 40)
    assert result == {"text": "", "session_id": "s" * 40, "auth_required": [{
        "provider": CONNECTION, "tool": "context_layer", "url": AUTH_URL,
        "scopes": ["openid", "email", "profile"], "agent_id": agent.id,
    }]}
    assert account.data.ops("invoke_harness") == []
    assert account.control.calls == []  # not even the deployed configuration
    assert _vault_calls(account) == []  # gateway mode: no COA token is fetched
    db = SessionLocal()
    try:
        (pending,) = db.query(OauthPendingSession).all()
    finally:
        db.close()
    assert (pending.agent_id, pending.caller_kind) == (f"gw:{GATEWAY_ID}", "user_jwt")
    assert minted == [("ada", "member", EMAIL, gw.CONSENT_JWT_MIN_VALID_S)]


def test_a_streamed_turn_without_consent_yields_only_the_card(account, minted, mcp):
    agent = _agent()
    _deploy(account, agent)
    events = list(chat_stream(agent, "q", session_id="s" * 40, actor_id="hr__ada",
                              runtime_user_id="ada", user_role="member", user_email=EMAIL,
                              workspace=_ws()))
    assert [e["event"] for e in events] == ["meta", "auth_required", "done"]
    assert events[1]["data"]["url"] == AUTH_URL
    assert events[1]["data"]["agent_id"] == agent.id
    assert account.data.ops("invoke_harness") == []


def test_a_consented_turn_swaps_the_gateway_for_the_members_jwt(account, minted, mcp):
    mcp.consented = True
    agent = _agent()
    _deploy(account, agent)
    result = _turn(agent)
    assert result["text"] == "5 days"
    kwargs = _invoked(account)
    assert kwargs["tools"] == [SWAPPED]
    assert kwargs["allowedTools"] == ["@launchpad_coa_gw"]
    assert kwargs["runtimeUserId"] == "ada"
    assert _vault_calls(account) == []
    # the swap re-read the deployed configuration and the gateway's trust
    assert {op for op, _ in account.control.calls} == {"get_harness", "get_gateway"}
    # the preflight and the swap use one cached JWT with the consent margin
    assert {call[3] for call in minted} == {gw.CONSENT_JWT_MIN_VALID_S}
    assert {call[:3] for call in minted} == {("ada", "member", EMAIL)}


def test_a_router_minted_token_rides_the_swap(account, minted, mcp):
    mcp.consented = True
    agent = _agent()
    _deploy(account, agent)
    _turn(agent, gateway_access_token="router-jwt")
    (tool,) = _invoked(account)["tools"]
    assert tool["config"]["remoteMcp"]["headers"] == {"Authorization": "Bearer router-jwt"}


def test_a_consented_stream_answers(account, minted, mcp):
    mcp.consented = True
    agent = _agent()
    _deploy(account, agent)
    events = list(chat_stream(agent, "q", actor_id="hr__ada", runtime_user_id="ada",
                              user_role="member", user_email=EMAIL, workspace=_ws()))
    assert [e["event"] for e in events] == ["meta", "delta", "done"]
    assert _invoked(account)["tools"] == [SWAPPED]


def test_an_explicit_allowed_tools_list_keeps_the_gateway_per_turn(account, minted, mcp):
    mcp.consented = True
    agent = _agent(allowed_tools=[])
    _deploy(account, agent)
    _turn(agent)
    assert _invoked(account)["allowedTools"] == ["@launchpad_coa_gw"]


def test_v1_and_a_caller_without_a_member_fail_closed(account, minted, mcp):
    mcp.consented = True
    agent = _agent()
    _deploy(account, agent)
    for kwargs in (
        {"runtime_user_id": "ada", "user_role": "member", "console_user": False},  # /v1
        {"runtime_user_id": "ada"},  # a caller that did not say the member's role
        {"user_role": "member"},  # nobody signed in
    ):
        with pytest.raises(AppError) as exc:
            invoke.invoke_agent_text(agent, "q", workspace=_ws(), **kwargs)
        assert exc.value.code == oauth_sessions.AS_USER_REQUIRES_CONSOLE
    events = list(chat_stream(agent, "q", console_user=False, workspace=_ws()))
    assert events[-1]["event"] == "error"
    assert events[-1]["data"]["code"] == oauth_sessions.AS_USER_REQUIRES_CONSOLE
    assert mcp.requests == [] and minted == []
    assert account.data.calls == []


def test_a_deployed_configuration_without_the_gateway_fails_closed(account, minted, mcp):
    mcp.consented = True
    agent = _agent()
    account.control.harness = {"tools": [], "allowedTools": []}  # deployed in direct mode
    with pytest.raises(AppError) as exc:
        _turn(agent)
    assert exc.value.code == "context_layer.gateway_unavailable"
    assert "re-publish" in exc.value.message
    assert account.data.ops("invoke_harness") == []


def test_a_gateway_that_no_longer_trusts_members_fails_closed(account, minted, mcp):
    """Never COA's gateway with the shared M2M identity: no swap, no turn."""
    mcp.consented = True
    agent = _agent()
    _deploy(account, agent)
    jwt = account.control.gateways[GATEWAY_ID]["authorizerConfiguration"]["customJWTAuthorizer"]
    jwt["allowedAudience"] = ["somebody-else"]
    with pytest.raises(AppError) as exc:
        _turn(agent)
    assert exc.value.code == "context_layer.gateway_unavailable"
    assert account.data.ops("invoke_harness") == []


def test_a_server_that_only_bears_the_alias_fails_closed(account, minted, mcp):
    """Swapped means the member's JWT on the alias, not just a remote_mcp under it."""
    mcp.consented = True
    agent = _agent()
    account.control.harness = {"tools": [{
        "type": "remote_mcp", "name": "launchpad_coa_gw",
        "config": {"remoteMcp": {"url": "https://mcp.example.com/mcp"}},
    }], "allowedTools": ["@launchpad_coa_gw"]}
    with pytest.raises(AppError) as exc:
        _turn(agent)
    assert exc.value.code == "context_layer.gateway_unavailable"
    assert account.data.ops("invoke_harness") == []


def test_a_direct_agent_is_unchanged_with_the_gateway_enabled(account, minted, mcp):
    account.data.token = fx.jwt(time.time() + 3600)
    agent = _agent(NS, name="hr-direct")
    account.control.harness = {"tools": [], "allowedTools": []}
    _turn(agent)
    kwargs = _invoked(account)
    assert [tool["name"] for tool in kwargs["tools"]] == ["coa"]
    assert kwargs["tools"][0]["config"]["remoteMcp"]["headers"] == {
        "Authorization": f"Bearer {account.data.token}"
    }
    assert kwargs["allowedTools"] == ["@coa"]
    assert mcp.requests == [] and minted == []  # no preflight, no pool JWT


# ── the console routes ───────────────────────────────────────────────────────


def test_the_chat_route_ends_at_the_card_then_answers(client, account, minted, mcp):
    agent = _agent()
    _deploy(account, agent)
    response = client.post(f"/api/chat/{agent.id}", json={"prompt": "q"})
    assert response.status_code == 200
    frames = [line for line in response.text.splitlines() if line.startswith("event:")]
    assert "event: auth_required" in frames and "event: delta" not in frames
    assert account.data.ops("invoke_harness") == []
    # the route minted the member's JWT with the gate's margin, for their own role
    assert minted[0] == ("admin", "admin", None, gw.CONSENT_JWT_MIN_VALID_S)
    db = SessionLocal()
    try:
        rows = db.query(ChatMessage).filter(ChatMessage.agent_id == agent.id).all()
    finally:
        db.close()
    assert [(r.role, r.text, r.name) for r in rows if r.role == "auth"] == [
        ("auth", CONNECTION, "context_layer")
    ]

    mcp.consented = True
    answered = client.post(f"/api/chat/{agent.id}", json={"prompt": "q"})
    assert "5 days" in answered.text
    kwargs = _invoked(account)
    assert kwargs["tools"] == [SWAPPED] and kwargs["runtimeUserId"] == "admin"
    assert USER_JWT not in answered.text


def test_the_console_invoke_route_names_the_missing_consent(client, account, minted, mcp):
    agent = _agent()
    _deploy(account, agent)
    response = client.post(f"/api/agents/{agent.id}/invoke", json={"prompt": "q"})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "context_layer.authorization_required"
    assert body["detail"]["authorization_url"] == AUTH_URL
    assert account.data.ops("invoke_harness") == []

    mcp.consented = True
    answered = client.post(f"/api/agents/{agent.id}/invoke",
                           json={"prompt": "q", "actor_id": "someone-else"})
    assert answered.status_code == 200, answered.text
    assert answered.json()["text"] == "5 days"
    assert _invoked(account)["tools"] == [SWAPPED]
    assert {call[:2] for call in minted} == {("admin", "admin")}
    assert USER_JWT not in json.dumps(answered.json())
