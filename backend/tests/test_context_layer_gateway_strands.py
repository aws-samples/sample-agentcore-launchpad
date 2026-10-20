"""Strands (``zip_runtime``) agents in gateway mode (``context_layer.via = "gateway"``,
task ``10-20-gw-strands``): the deploy expansion (no COA tool; the managed gateway's
URL baked as ``CONTEXT_GATEWAY``; the ``coa___`` prompt), direct-mode output unchanged,
the refusals without the gateway, the generated runtime's client (the member's JWT, no
M2M fallback, the ``coa___*`` filter, next to launchpad-gw), the consent gate on the
stream and text paths, the member's JWT on the payload, the routes and the code view.

Hermetic: AWS is ``coa_fakes`` + the gateway control plane of
``test_context_layer_gateway``; the gateway's MCP endpoint is the consent tests'
``GatewayMcp`` behind an ``httpx.MockTransport``; InvokeAgentRuntime is a fake that
records the payload and answers like a generated runtime.
"""

import asyncio
import importlib.util
import io
import json
import types
from contextlib import ExitStack
from typing import Any

import httpx
import mcp.client.streamable_http as mcp_http
import pytest
import strands
import strands.tools.mcp as strands_mcp

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.deployer import zip_runtime
from app.deployer.environment import runtime_environment
from app.deployer.pipeline import StageContext
from app.deployer.return_url import register_return_url_stage
from app.models.ledger import Agent, ChatMessage, OauthPendingSession
from app.routers import agents as agents_router
from app.schemas.agent import AgentSpec, ToolRef
from app.services import agent_iam, context_layer, invoke, oauth_sessions, policy_identity
from app.services import context_layer_gateway as gw
from app.services.chat import chat_stream
from app.services.workspace import WorkspaceContext, context_for_workspace
from app.templates import context_layer_support as support
from app.templates import strands_agent
from app.templates.identity_support import (
    as_user_connections_stored,
    render_identity_source,
    uses_identity,
)
from app.templates.strands_agent import render_main_py

from . import coa_fakes as fx
from .conftest import set_default_resources
from .test_agent_source import StubControl, StubS3, _runtime, _zip
from .test_context_layer_gateway import RESOURCES, GatewayControl
from .test_context_layer_gateway_consent import (
    AUTH_URL,
    GATEWAY_ID,
    GATEWAY_URL,
    USER_JWT,
    GatewayMcp,
)
from .test_context_layer_gateway_consent import REF as GATEWAY_REF
from .test_context_layer_gateway_harness import EMAIL, NS, VIA_GATEWAY

CONNECTION = "coa-coa-w2"
NAME = "hr-gw-z"
RUNTIME_ARN = f"arn:aws:bedrock-agentcore:{fx.WS_REGION}:{fx.ACCOUNT}:runtime/hr_gw_z_ab12cd-Xy1"
BAKED = f"CONTEXT_GATEWAY = {{'url': '{GATEWAY_URL}', 'tools': 'coa___*'}}"
CALL = "+ context_gateway_tools(stack, gateway_access_token)"
SCOPES = ["openid", "email", "profile"]


class RuntimeData(fx.FakeData):
    """The data plane plus InvokeAgentRuntime: each call recorded with its payload
    decoded, answered as a generated runtime that predates streaming would."""

    def invoke_agent_runtime(self, **params):
        self.calls.append(("invoke_agent_runtime", {
            **params, "payload": json.loads(params["payload"]),
        }))
        body = json.dumps({"result": "5 days"}).encode()
        return {"response": io.BytesIO(body), "contentType": "application/json"}


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
    account.data = RuntimeData()
    fx.install(monkeypatch, account)
    fx.connect_default(account)
    set_default_resources(RESOURCES)
    _set_ref(GATEWAY_REF)
    account.control.calls.clear()
    account.data.calls.clear()
    return account


@pytest.fixture
def launched(monkeypatch):
    jobs: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: jobs.append(jid))
    return jobs


def _ws():
    return context_for_workspace("default")


def _set_ref(ref: dict | None) -> None:
    db = SessionLocal()
    try:
        gw._save_ref(db, "default", ref)
    finally:
        db.close()


def _stored(ref: dict | None = None, **fields) -> dict:
    """The spec a create stores for a Strands agent attaching the layer."""
    return AgentSpec(name=NAME, method="zip_runtime", system_prompt="help",
                     context_layer=VIA_GATEWAY if ref is None else ref, **fields).model_dump()


def _agent(ref: dict | None = None, **fields) -> Agent:
    db = SessionLocal()
    try:
        row = Agent(workspace_id="default", name=NAME, method="zip_runtime", status="active",
                    spec=_stored(ref, **fields), arn=RUNTIME_ARN,
                    resource_id="hr_gw_z_ab12cd-Xy1", version="1")
        db.add(row)
        db.commit()
        db.refresh(row)
        db.expunge(row)
        return row
    finally:
        db.close()


def _stage_ctx(agent: Agent) -> StageContext:
    return StageContext(agent_id=agent.id, deployment_id="dep-1", job_id="job-1",
                        workspace=_ws())


def _turn(agent: Agent, **kwargs):
    return invoke.invoke_agent_text(
        agent, "EMP-3105 还能申请几天年假？", runtime_user_id="ada", user_role="member",
        user_email=EMAIL, workspace=_ws(), **kwargs,
    )


def _invoked(account) -> dict[str, Any]:
    (call,) = account.data.ops("invoke_agent_runtime")
    return call


def _vault_calls(account) -> list:
    return [op for op, _ in account.data.calls if op != "invoke_agent_runtime"]


# ── deploy: the expansion ────────────────────────────────────────────────────


def test_gateway_mode_bakes_the_managed_gateway_and_no_coa_tool(account):
    spec = context_layer.deploy_spec(_stored(), "default")
    assert spec.tools == []  # no as_user `coa` tool: no identity client, no gates_turn
    assert not uses_identity(spec) and render_identity_source(spec) == ""
    assert support.bound_gateway_url(spec) == GATEWAY_URL
    # the URL rides the deploy only: never serialized onto the spec
    assert spec.model_dump()["context_layer"] == VIA_GATEWAY
    code = render_main_py(spec)
    assert BAKED in code and CALL in code
    assert "# <launchpad-context-gateway-mcp:v1>" in code
    assert "IDENTITY_AUTH_TOOLS" not in code and "__LAUNCHPAD_" not in code
    compile(code, "main.py", "exec")
    assert account.control.calls == []  # ledger identifiers only, no AWS read


def test_the_prompt_names_the_gateways_tools(account):
    prompt = context_layer.deploy_spec(_stored(), "default").system_prompt
    assert prompt == "help" + support.prompt_addendum(
        fx.NAMESPACE, "AnyCompany HR", tool_prefix="coa___",
    )
    for tool in support.COA_TOOLS:
        assert f"`coa___{tool}`" in prompt
    assert "`list_metrics`" not in prompt and "launchpad_coa_gw_" not in prompt
    assert support.strands_tool_prefix(AgentSpec(**_stored())) == "coa___"
    assert support.strands_tool_prefix(AgentSpec(**_stored(NS))) == ""


def test_nothing_else_is_deployed_for_coa(account):
    """No role grant, environment entry or return URL: the runtime calls the gateway
    over plain HTTPS with the member's JWT, which the gateway's authorizer checks."""
    spec = context_layer.deploy_spec(_stored(), "default")
    plain = AgentSpec(name=NAME, method="zip_runtime", system_prompt="help")
    ctx = agent_iam.role_context(_ws())
    assert agent_iam.policy_document(spec, ctx) == agent_iam.policy_document(plain, ctx)
    resources = _ws().resources
    assert runtime_environment(spec, resources, agent_id="a1") == runtime_environment(
        plain, resources, agent_id="a1",
    )
    assert register_return_url_stage(account.control, spec, "hr_gw_z_ab12cd-Xy1",
                                     lambda _m: None) is True
    assert account.control.calls == []


def test_a_client_cannot_name_the_gateway_url():
    spec = AgentSpec(**{**_stored(), "context_layer": {
        **VIA_GATEWAY, "_gateway_url": "https://evil.example/mcp",
        "gateway_url": "https://evil.example/mcp",
    }})
    assert support.bound_gateway_url(spec) == ""
    assert spec.model_dump()["context_layer"] == VIA_GATEWAY


def test_a_gateway_spec_renders_only_from_its_expansion():
    """A wiring bug fails loudly: never an agent deployed in the wrong mode."""
    with pytest.raises(ValueError, match="deploy expansion"):
        render_main_py(AgentSpec(**_stored()))


@pytest.mark.parametrize("ref", [NS, {**NS, "via": "direct"}], ids=["omitted", "direct"])
def test_a_direct_strands_agent_deploys_exactly_as_before(account, ref, tmp_path, monkeypatch):
    """Golden: the direct expansion, its baked COA tool, main.py and the environment are
    what they were before gateway mode, whether or not the managed gateway is enabled."""
    layer = context_layer.require_connected("default")
    spec = context_layer.deploy_spec(_stored(ref), "default")
    assert [(t.name, t.type, t.auth.mode) for t in spec.tools] == [("coa", "mcp", "as_user")]
    assert support.bound_gateway_url(spec) == ""
    code = render_main_py(spec)
    baked = [{
        "name": "coa", "type": "mcp", "url": layer.mcp_url, "description": "",
        "method": "GET", "body": "", "headers": {}, "connection": CONNECTION,
        "kind": "oauth2", "mode": "as_user", "scopes": SCOPES, "audience": "",
        "key_in": "header", "key_name": "Authorization", "gates_turn": True,
    }]
    assert f"IDENTITY_AUTH_TOOLS = {baked!r}" in code
    for text in ("CONTEXT_GATEWAY", "context_gateway_tools", "launchpad-context-gateway"):
        assert text not in code
    env = runtime_environment(spec, _ws().resources, agent_id="a1")

    _set_ref(None)
    again = context_layer.deploy_spec(_stored(ref), "default")
    assert render_main_py(again) == code
    assert runtime_environment(again, _ws().resources, agent_id="a1") == env

    # byte-identical to the template without the gateway-mode lines, i.e. to what
    # every agent rendered before they existed: dropping them leaves no residue
    template = (strands_agent.TEMPLATE_DIR / "main.py.tmpl").read_text(encoding="utf-8")
    lines = template.splitlines(keepends=True)
    before = [line for line in lines if "__LAUNCHPAD_CONTEXT_GATEWAY_" not in line]
    assert len(lines) - len(before) == 2
    (tmp_path / "main.py.tmpl").write_text("".join(before), encoding="utf-8")
    monkeypatch.setattr(strands_agent, "TEMPLATE_DIR", tmp_path)
    assert render_main_py(spec) == code


def test_an_agent_without_a_layer_renders_no_context_gateway():
    code = render_main_py(AgentSpec(name=NAME, method="zip_runtime", system_prompt="help"))
    assert "CONTEXT_GATEWAY" not in code and "context_gateway_tools" not in code
    assert "__LAUNCHPAD_" not in code


# ── deploy: the stages and the refusals ──────────────────────────────────────


def test_generate_builds_with_the_managed_gateway(account):
    agent = _agent()
    ctx = _stage_ctx(agent)
    zip_runtime._stage_generate(ctx, agent)
    assert BAKED in ctx.scratch["code"] and CALL in ctx.scratch["code"]


@pytest.mark.parametrize("ref", [None, {**GATEWAY_REF, "gateway_url": "http://gw.example/mcp"}],
                         ids=["disabled", "not-https"])
def test_no_usable_managed_gateway_fails_generate(account, ref):
    agent = _agent()
    _set_ref(ref)
    ctx = _stage_ctx(agent)
    with pytest.raises(AppError) as exc:
        zip_runtime._stage_generate(ctx, agent)
    assert exc.value.code == "context_layer.gateway_unavailable"
    assert exc.value.status_code == 409
    assert "code" not in ctx.scratch
    # every later stage builds from the same expansion, so none can either
    with pytest.raises(AppError):
        context_layer.deploy_spec(agent.spec, "default")


def _body(ref: dict, name: str = NAME) -> dict:
    return {"name": name, "method": "zip_runtime", "system_prompt": "help",
            "context_layer": ref}


def test_create_in_gateway_mode_needs_the_gateway(client, account, launched):
    _set_ref(None)
    response = client.post("/api/agents", json=_body(VIA_GATEWAY))
    assert response.status_code == 409
    assert response.json()["code"] == "context_layer.gateway_unavailable"
    assert launched == []

    _set_ref(GATEWAY_REF)
    created = client.post("/api/agents", json=_body(VIA_GATEWAY))
    assert created.status_code == 202, created.text
    assert created.json()["agent"]["spec"]["context_layer"] == VIA_GATEWAY
    assert len(launched) == 1
    db = SessionLocal()
    try:
        assert [a["name"] for a in gw.gateway_agents(db, "default")] == [NAME]
    finally:
        db.close()


# ── the generated runtime ────────────────────────────────────────────────────


class _Tool:
    def __init__(self, name: str):
        self.tool_name = name


class _FakeMcpClient:
    """strands.tools.mcp.MCPClient: opens the transport on start, lists tools."""

    instances: list["_FakeMcpClient"] = []
    tools = ("coa___list_metrics", "coa___query", "crm___lookup")

    def __init__(self, transport):
        self.transport, self.stopped = transport, False
        _FakeMcpClient.instances.append(self)

    def start(self):
        self.transport()
        return self

    def list_tools_sync(self):
        return [_Tool(name) for name in self.tools]

    def stop(self, *_exc):
        self.stopped = True


@pytest.fixture
def runtime(account, tmp_path, monkeypatch):
    """The generated main.py of a gateway-mode agent that also attaches launchpad-gw,
    imported with a stub strands Agent and a fake MCP client + transport."""
    spec = context_layer.deploy_spec(
        _stored(tools=[ToolRef(type="gateway", name="launchpad-gw")]), "default",
    )
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    target = tmp_path / "hr_gw_z_main.py"
    target.write_text(render_main_py(spec), encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location(target.stem, target)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(module, "memory_session_manager", lambda _a, _s: None)
    opened: list[tuple[str, dict]] = []
    monkeypatch.setattr(mcp_http, "streamablehttp_client",
                        lambda url, **kw: opened.append((url, kw)) or "transport")
    monkeypatch.setattr(strands_mcp, "MCPClient", _FakeMcpClient)
    _FakeMcpClient.instances = []
    module.opened = opened
    return module


def test_the_runtime_loads_coas_tools_as_the_member(runtime):
    assert runtime.CONTEXT_GATEWAY == {"url": GATEWAY_URL, "tools": "coa___*"}
    with ExitStack() as stack:
        tools = runtime.context_gateway_tools(stack, "member-jwt")
        assert [t.tool_name for t in tools] == ["coa___list_metrics", "coa___query"]
        (client,) = _FakeMcpClient.instances
        assert client.stopped is False  # open while the agent may call a tool
    assert client.stopped is True
    ((url, kwargs),) = runtime.opened
    assert url == GATEWAY_URL
    assert kwargs["headers"] == {"Authorization": "Bearer member-jwt"}
    http = kwargs["httpx_client_factory"]()
    try:
        assert http.follow_redirects is False  # the JWT goes to the gateway only
    finally:
        asyncio.run(http.aclose())


@pytest.mark.parametrize("token", [None, ""])
def test_no_member_token_means_no_coa_tools(runtime, token):
    """No machine-identity fallback toward COA's gateway."""
    assert runtime.context_gateway_tools(ExitStack(), token) == []
    assert _FakeMcpClient.instances == [] and runtime.opened == []


def test_a_failing_gateway_session_leaves_the_turn_without_coa(runtime, monkeypatch):
    def refuse(self):
        raise RuntimeError("gateway unreachable")

    monkeypatch.setattr(_FakeMcpClient, "start", refuse)
    with ExitStack() as stack:
        assert runtime.context_gateway_tools(stack, "member-jwt") == []
    assert _FakeMcpClient.instances[0].stopped is True


def test_the_turn_hands_the_payload_token_to_both_gateways(runtime, monkeypatch):
    seen: dict[str, Any] = {}

    def launchpad_gw(_stack, token=None):
        seen["launchpad-gw"] = token
        return []

    def coa_gateway(_stack, token=None):
        seen["coa"] = token
        return [_Tool("coa___query")]

    monkeypatch.setattr(runtime, "GATEWAY_TOOLS", launchpad_gw)
    monkeypatch.setattr(runtime, "context_gateway_tools", coa_gateway)

    def build_agent(actor_id, session_id, extra_tools=None, session_manager=None):
        seen["tools"] = [t.tool_name for t in extra_tools]

        async def stream_async(prompt):
            yield {"data": "an answer"}
            yield {"result": "an answer"}

        return types.SimpleNamespace(stream_async=stream_async, system_prompt="")

    monkeypatch.setattr(runtime, "build_agent", build_agent)
    monkeypatch.setattr(runtime, "recall_context", lambda *_a: "")

    async def collect(payload):
        return [event async for event in runtime.invoke(payload)]

    events = asyncio.run(collect({"prompt": "q", "gateway_access_token": "member-jwt"}))
    assert seen == {"launchpad-gw": "member-jwt", "coa": "member-jwt",
                    "tools": ["coa___query"]}
    assert [e["event"] for e in events] == ["delta", "complete"]
    seen.clear()
    asyncio.run(collect({"prompt": "q"}))
    assert seen["coa"] is None


# ── the turn ─────────────────────────────────────────────────────────────────


def test_a_turn_without_consent_ends_at_the_card(account, minted, mcp):
    agent = _agent()
    result = _turn(agent, session_id="s" * 40)
    assert result == {"text": "", "session_id": "s" * 40, "auth_required": [{
        "provider": CONNECTION, "tool": "context_layer", "url": AUTH_URL,
        "scopes": SCOPES, "agent_id": agent.id,
    }]}
    assert account.data.ops("invoke_agent_runtime") == []
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
    events = list(chat_stream(agent, "q", session_id="s" * 40, actor_id="hr__ada",
                              runtime_user_id="ada", user_role="member", user_email=EMAIL,
                              workspace=_ws()))
    assert [e["event"] for e in events] == ["meta", "auth_required", "done"]
    assert events[1]["data"]["url"] == AUTH_URL
    assert events[1]["data"]["agent_id"] == agent.id
    assert account.data.ops("invoke_agent_runtime") == []


def test_a_consented_turn_carries_the_members_jwt(account, minted, mcp):
    mcp.consented = True
    agent = _agent()
    result = _turn(agent)
    assert result["text"] == "5 days"
    call = _invoked(account)
    assert call["payload"]["gateway_access_token"] == USER_JWT
    assert call["runtimeUserId"] == "ada"
    assert "force_reauth_providers" not in call["payload"]
    assert _vault_calls(account) == []
    assert account.control.calls == []  # nothing to swap: the URL is in the code
    # the preflight and the payload carry one cached JWT with the consent margin
    assert set(minted) == {("ada", "member", EMAIL, gw.CONSENT_JWT_MIN_VALID_S)}


def test_a_consented_stream_answers(account, minted, mcp):
    mcp.consented = True
    agent = _agent()
    events = list(chat_stream(agent, "q", actor_id="hr__ada", runtime_user_id="ada",
                              user_role="member", user_email=EMAIL, workspace=_ws()))
    assert [e["event"] for e in events] == ["meta", "delta", "done"]
    assert _invoked(account)["payload"]["gateway_access_token"] == USER_JWT


def test_a_router_minted_token_rides_the_payload(account, minted, mcp):
    mcp.consented = True
    agent = _agent()
    _turn(agent, gateway_access_token="router-jwt")
    assert _invoked(account)["payload"]["gateway_access_token"] == "router-jwt"


def test_v1_and_a_caller_without_a_member_fail_closed(account, minted, mcp):
    mcp.consented = True
    agent = _agent()
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


def test_a_gateway_deleted_out_of_band_fails_closed(account, minted, mcp):
    agent = _agent()
    _set_ref(None)
    with pytest.raises(AppError) as exc:
        _turn(agent)
    assert exc.value.code == "context_layer.gateway_not_enabled"
    assert mcp.requests == [] and account.data.calls == []


def test_a_direct_strands_agent_is_unchanged_with_the_gateway_enabled(account, minted, mcp):
    agent = _agent(NS)
    _turn(agent)
    call = _invoked(account)
    assert "gateway_access_token" not in call["payload"]
    assert call["runtimeUserId"] == "ada"
    assert mcp.requests == [] and minted == []  # no preflight, no pool JWT


def test_revocations_are_not_forwarded_in_gateway_mode(account):
    """Its consent is the gateway's (not revocable here): no as_user tool to re-ask."""
    assert as_user_connections_stored(_stored(), CONNECTION) == []
    assert as_user_connections_stored(_stored(NS), CONNECTION) == [CONNECTION]
    gateway_agent = _agent()
    db = SessionLocal()
    try:
        oauth_sessions.revoke(db, "default", user_id="ada", provider=CONNECTION)
    finally:
        db.close()
    assert invoke._pending_force_reauth(gateway_agent, "ada") == []


# ── the console routes ───────────────────────────────────────────────────────


def test_the_chat_route_ends_at_the_card_then_answers(client, account, minted, mcp):
    agent = _agent()
    response = client.post(f"/api/chat/{agent.id}", json={"prompt": "q"})
    assert response.status_code == 200
    frames = [line for line in response.text.splitlines() if line.startswith("event:")]
    assert "event: auth_required" in frames and "event: delta" not in frames
    assert account.data.ops("invoke_agent_runtime") == []
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
    call = _invoked(account)
    assert call["payload"]["gateway_access_token"] == USER_JWT
    assert call["runtimeUserId"] == "admin"
    assert USER_JWT not in answered.text


def test_the_console_invoke_route_names_the_missing_consent(client, account, minted, mcp):
    agent = _agent()
    response = client.post(f"/api/agents/{agent.id}/invoke", json={"prompt": "q"})
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "context_layer.authorization_required"
    assert body["detail"]["authorization_url"] == AUTH_URL
    assert account.data.ops("invoke_agent_runtime") == []

    mcp.consented = True
    answered = client.post(f"/api/agents/{agent.id}/invoke",
                           json={"prompt": "q", "actor_id": "someone-else"})
    assert answered.status_code == 200, answered.text
    assert answered.json()["text"] == "5 days"
    assert _invoked(account)["payload"]["gateway_access_token"] == USER_JWT
    assert {call[:2] for call in minted} == {("admin", "admin")}
    assert USER_JWT not in json.dumps(answered.json())


def test_the_code_view_shows_the_context_gateway(client, account, monkeypatch):
    """R4: main.py, read back from the deployed package, names the managed gateway."""
    code = render_main_py(context_layer.deploy_spec(_stored(), "default"))
    key = f"agents/{NAME}/deployment_package.zip"
    s3 = StubS3({key: _zip({"main.py": code, "requirements.txt": "strands-agents\n"})})
    monkeypatch.setattr(agents_router, "control_client",
                        lambda _ws=None: StubControl(_runtime(key)))
    monkeypatch.setattr(WorkspaceContext, "client", lambda self, service, **kw: s3)
    agent = _agent()
    body = client.get(f"/api/agents/{agent.id}/source").json()
    main = body["files"][0]
    assert main["path"] == "main.py"
    assert BAKED in main["content"] and CALL in main["content"]
    assert USER_JWT not in main["content"]
    assert s3.gets and {g["Key"] for g in s3.gets} == {key}
