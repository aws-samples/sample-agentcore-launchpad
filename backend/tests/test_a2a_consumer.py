"""N10v2-bug5 — the A2A *consumer* path: an agent mounts an APPROVED A2A record of this
workspace and asks the agent behind it a question.

(e) spec: ``ToolRef.type="a2a"`` (harness + zip_runtime/http only, legacy specs untouched)
(a) ``GET /api/registry/attachables`` → ``agents`` (APPROVED A2A records joined to the ledger)
(c) Strands template + self-built Harness graft an ``ask_<peer>`` tool that calls the peer
    runtime through ``bedrock-agentcore:InvokeAgentRuntime`` with the A2A JSON-RPC envelope
(d) the consuming agent's role gets ``InvokeAgentRuntime`` on exactly the mounted runtimes
"""

import json

import pytest
from pydantic import ValidationError

import app.routers.registry as registry_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer.harness_local import TEMPLATE_DIR, check_template, render_harness_bundle
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import agent_iam
from app.services import registry_backend as reg
from app.services import registry_console as console
from app.services.registry_local import LocalRegistryBackend
from app.templates import a2a_support
from app.templates.strands_agent import render_main_py
from tests.conftest import set_default_resources, ws_ctx
from tests.test_agent_iam_policy import CTX, _sids, _statement
from tests.test_strands_template import _import_rendered

WS = ws_ctx({"artifacts_bucket": "bkt", "registry_id": "local-default"},
            region="cn-northwest-1", id=DEFAULT_WORKSPACE_ID)
PEER_ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/aurora_faq-abc123"
PEER2_ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/hr_desk-def456"
PEER_ID = "agent0001-aaaa-bbbb-cccc-000000000001"


def _a2a_ref(name="aurora-faq-a2a", arn=PEER_ARN, record_id="rec-1", **extra):
    return {"type": "a2a", "name": name,
            "config": {"record_id": record_id, "runtime_arn": arn, "agent_id": PEER_ID,
                       "url": "https://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/x",
                       "description": "Answers Aurora product questions", **extra}}


def _spec(**over) -> AgentSpec:
    base = {"name": "consumer", "method": "harness", "system_prompt": "You route.",
            "tools": [_a2a_ref()]}
    return AgentSpec(**{**base, **over})


# ── (e) spec contract ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", ["harness", "zip_runtime"])
def test_a2a_tool_refs_are_accepted_for_the_code_generating_methods(method):
    spec = _spec(method=method)
    (tool,) = spec.tools
    assert tool.type == "a2a" and tool.config["runtime_arn"] == PEER_ARN
    assert a2a_support.mounted_agents(spec)[0]["tool_name"] == "ask_aurora_faq_a2a"


def test_legacy_specs_round_trip_unchanged():
    legacy = {"name": "old", "method": "zip_runtime", "system_prompt": "p",
              "tools": [{"type": "gateway", "name": "hr-database",
                         "config": {"record_id": "r", "gateway_id": "abc123"}}]}
    assert AgentSpec(**legacy).model_dump()["tools"] == legacy["tools"]
    assert a2a_support.mounted_agents(AgentSpec(**legacy)) == []


@pytest.mark.parametrize("over, needle", [
    ({"method": "zip_runtime", "protocol": "a2a"}, "protocol=a2a"),
    ({"method": "studio", "code": "print(1)"}, "code"),
    ({"method": "byoc", "system_prompt": "",
      "byoc": {"artifact_kind": "image", "image_uri": "123456789012.dkr.ecr.cn-northwest-1."
               "amazonaws.com.cn/launchpad-agents:x"}}, "byoc"),
    ({"tools": [_a2a_ref(arn="arn:aws-cn:s3:::not-a-runtime")]}, "runtime_arn"),
    ({"tools": [_a2a_ref(record_id="")]}, "record_id"),
    ({"tools": [_a2a_ref(), _a2a_ref(arn=PEER2_ARN, record_id="rec-2")]}, "unique"),
    ({"tools": [_a2a_ref(name="Bad Name!")]}, "name"),
])
def test_a2a_tool_refs_are_refused_where_nothing_could_render_them(over, needle):
    with pytest.raises(ValidationError) as excinfo:
        _spec(**over)
    assert needle in str(excinfo.value)


# ── (a) attachables ────────────────────────────────────────────────────────────


@pytest.fixture
def backend(client):
    impl = LocalRegistryBackend()
    reg.set_backend(impl)
    registry_router._attachables_cache.clear()
    set_default_resources(dict(WS.resources))
    return impl


def _a2a_record(backend, name, arn, *, approve=True, skills=None):
    card = reg.build_a2a_card(name=name, description=f"{name} card", arn=arn, method="zip_runtime",
                              skills=skills or [{"id": "faq", "name": "FAQ", "description": "d"}],
                              transport="a2a-jsonrpc", workspace=WS)
    record, _ = backend.upsert_record(WS, name=name, description=f"{name} agent",
                                      descriptor_type="A2A",
                                      descriptors=reg.build_a2a_descriptors(card))
    backend.submit_record(WS, record["recordId"])
    if approve:
        backend.approve_record(WS, record["recordId"])
    return record["recordId"], card


def _ledger_agent(record_id, *, arn=PEER_ARN, status="active", protocol="a2a",
                  agent_id=PEER_ID, name="aurora-faq-a2a"):
    db = SessionLocal()
    try:
        db.add(Agent(id=agent_id, workspace_id=DEFAULT_WORKSPACE_ID, name=name,
                     method="zip_runtime", status=status, arn=arn,
                     spec={"protocol": protocol, "system_prompt": "x"},
                     registry_record_id=record_id))
        db.commit()
    finally:
        db.close()


def test_attachables_offer_approved_a2a_records_joined_to_the_ledger(backend):
    rid, card = _a2a_record(backend, "aurora-faq-a2a", PEER_ARN)
    _ledger_agent(rid)
    draft, _ = _a2a_record(backend, "draft-agent", PEER2_ARN, approve=False)
    orphan, _ = _a2a_record(backend, "orphan-agent", PEER2_ARN)  # no ledger agent owns it

    catalog = console.attachable_records(WS, gateways=[])

    assert catalog["mcp_servers"] == [] and catalog["skills"] == []
    by_name = {a["name"]: a for a in catalog["agents"]}
    assert set(by_name) == {"aurora-faq-a2a", "orphan-agent"}
    assert by_name["aurora-faq-a2a"] == {
        "name": "aurora-faq-a2a", "description": "aurora-faq-a2a agent", "record_id": rid,
        "agent_id": PEER_ID, "runtime_arn": PEER_ARN, "url": card["url"],
        "skills": [{"id": "faq", "name": "FAQ", "description": "d"}],
        "attachable": True, "attachability_reason": None,
    }
    assert by_name["orphan-agent"]["attachable"] is False
    assert by_name["orphan-agent"]["runtime_arn"] is None
    assert "no Launchpad agent" in by_name["orphan-agent"]["attachability_reason"]


def test_an_a2a_record_whose_agent_is_not_live_is_listed_unattachable(backend):
    rid, _ = _a2a_record(backend, "aurora-faq-a2a", PEER_ARN)
    _ledger_agent(rid, status="failed")
    (row,) = console.attachable_records(WS, gateways=[])["agents"]
    assert row["attachable"] is False and "not active" in row["attachability_reason"]


def test_attachables_route_carries_the_agents_key(client, backend, monkeypatch):
    rid, _ = _a2a_record(backend, "aurora-faq-a2a", PEER_ARN)
    _ledger_agent(rid)
    monkeypatch.setattr(console, "_list_live_mcp_gateways", lambda _ws: [])
    body = client.get("/api/registry/attachables").json()
    assert set(body) == {"mcp_servers", "skills", "agents"}
    assert body["agents"][0]["record_id"] == rid


# ── (c) Strands template ──────────────────────────────────────────────────────


class _DataPlane:
    """bedrock-agentcore data-plane stub: records InvokeAgentRuntime, answers a Task."""

    def __init__(self, reply="30 days.", fail=False):
        self.reply, self.fail, self.calls = reply, fail, []
        self.result = None  # a full Task/Message result to answer instead of `reply`

    def invoke_agent_runtime(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("AccessDeniedException: not allowed")
        body = {"jsonrpc": "2.0", "id": "x", "result": self.result or {
            "kind": "task", "history": [{"parts": [{"kind": "text", "text": "30 d"}]}],
            "artifacts": [{"parts": [{"kind": "text", "text": self.reply}]}]}}

        class _Stream:
            def read(self_inner):
                return json.dumps(body).encode()
        return {"response": _Stream()}


@pytest.fixture
def consumer_module(tmp_path, monkeypatch):
    spec = _spec(method="zip_runtime", tools=[_a2a_ref(), _a2a_ref(
        name="hr-desk", arn=PEER2_ARN, record_id="rec-2", description="HR questions")])
    module = _import_rendered(spec, tmp_path, monkeypatch, "a2a_consumer")
    plane = _DataPlane()
    monkeypatch.setattr(module.boto3, "client", lambda *a, **k: plane)
    return module, plane


def test_strands_template_bakes_one_ask_tool_per_mounted_agent(consumer_module):
    module, plane = consumer_module
    names = [t.tool_name for t in module.A2A_TOOLS]
    assert names == ["ask_aurora_faq_a2a", "ask_hr_desk"]
    descriptions = module.DEFAULT_TOOL_DESCRIPTIONS
    assert "Answers Aurora product questions" in descriptions["ask_aurora_faq_a2a"]
    assert "ask_hr_desk" in module.DEFAULT_SYSTEM_PROMPT  # the prompt names the peers
    agent = module.build_agent("river", "sess-1")
    assert {t.tool_name for t in agent.tools} >= {"ask_aurora_faq_a2a", "ask_hr_desk"}


def test_ask_tool_sends_the_a2a_envelope_through_invoke_agent_runtime(consumer_module):
    module, plane = consumer_module
    module.a2a_bind_session("console-session-1")
    assert module.ask_aurora_faq_a2a("What is the refund window?") == "30 days."
    (call,) = plane.calls
    assert call["agentRuntimeArn"] == PEER_ARN
    assert len(call["runtimeSessionId"]) >= 33
    envelope = json.loads(call["payload"])
    assert envelope["method"] == "message/send" and envelope["jsonrpc"] == "2.0"
    message = envelope["params"]["message"]
    assert message["parts"] == [{"kind": "text", "text": "What is the refund window?"}]
    assert message["role"] == "user" and message["contextId"] == call["runtimeSessionId"]
    # N16-obs90: a peer never names a memory actor — the consumer sends no metadata at all
    assert "metadata" not in message
    # stable per (consumer session, peer); distinct across peers
    module.ask_aurora_faq_a2a("again")
    module.ask_hr_desk("hello")
    sids = [c["runtimeSessionId"] for c in plane.calls]
    assert sids[0] == sids[1] != sids[2]
    assert plane.calls[2]["agentRuntimeArn"] == PEER2_ARN
    # a new console session → a new peer session
    module.a2a_bind_session("console-session-2")
    module.ask_aurora_faq_a2a("again")
    assert plane.calls[3]["runtimeSessionId"] != sids[0]


def test_ask_tool_reports_a_peer_failure_to_the_model_instead_of_raising(consumer_module):
    module, plane = consumer_module
    plane.fail = True
    out = module.ask_aurora_faq_a2a("hi")
    assert out.startswith("[a2a error]") and "aurora-faq-a2a" in out


def test_ask_tool_relays_a_failed_task_status_instead_of_no_text(consumer_module):
    """N10v3-bug5 (secondary): the peer's failed Task carried the reason in
    status.message, but the model only heard "returned no text" three times."""
    module, plane = consumer_module
    plane.result = {"kind": "task", "artifacts": [], "status": {
        "state": "failed", "message": {"parts": [
            {"kind": "text", "text": "RuntimeError: no workload access token"}]}}}
    out = module.ask_aurora_faq_a2a("hi")
    assert out.startswith("[a2a error] aurora-faq-a2a")
    assert "failed" in out and "no workload access token" in out
    plane.result = {"kind": "task", "artifacts": [], "status": {"state": "completed"}}
    assert module.ask_aurora_faq_a2a("hi") == "[a2a] aurora-faq-a2a returned no text"
    # review F3: an input/auth request is the peer's reply, relayed as text
    for state in ("input-required", "auth-required"):
        plane.result = {"kind": "task", "artifacts": [], "status": {"state": state, "message": {
            "parts": [{"kind": "text", "text": "Which region?"}]}}}
        assert module.ask_aurora_faq_a2a("hi") == "Which region?"


def test_spec_without_a2a_renders_no_client_and_no_bind(tmp_path):
    spec = AgentSpec(name="plain", method="zip_runtime", system_prompt="p")
    source = render_main_py(spec)
    assert "def ask_" not in source and "A2A_TOOLS" in source  # the empty list stays


# ── (c) self-built Harness ────────────────────────────────────────────────────


def test_harness_bundle_grafts_the_a2a_client_module():
    ws = ws_ctx({}, region="cn-northwest-1")
    bundle = render_harness_bundle(_spec(), ws)
    main = bundle.files["main.py"]
    assert "launchpad_a2a_tools.py" in bundle.files
    assert "from launchpad_a2a_tools import A2A_TOOLS" in main
    assert "tools.extend([*A2A_TOOLS])" in main
    assert "def ask_aurora_faq_a2a(" in bundle.files["launchpad_a2a_tools.py"]
    assert bundle.notes["a2a_agents"].startswith("wired (1 ")
    plain = render_harness_bundle(
        AgentSpec(name="plain", method="harness", system_prompt="p"), ws)
    assert "launchpad_a2a_tools.py" not in plain.files and "A2A_TOOLS" not in plain.files["main.py"]


def test_real_harness_template_still_passes_the_self_check_with_the_a2a_probe():
    assert check_template(TEMPLATE_DIR) == []


# ── (d) IAM ───────────────────────────────────────────────────────────────────


def test_consumer_role_may_invoke_exactly_the_mounted_runtimes():
    spec = _spec(tools=[_a2a_ref(), _a2a_ref(name="hr-desk", arn=PEER2_ARN, record_id="rec-2"),
                        {"type": "builtin", "name": "code-interpreter"}])
    statement = _statement(spec, "A2AInvokeMountedPeers")
    # N12-bug5: the pair — an invoke carrying runtimeUserId is authorized as ForUser
    assert statement["Action"] == [
        "bedrock-agentcore:InvokeAgentRuntime", "bedrock-agentcore:InvokeAgentRuntimeForUser"]
    assert statement["Resource"] == [
        PEER_ARN, f"{PEER_ARN}/runtime-endpoint/*", PEER2_ARN, f"{PEER2_ARN}/runtime-endpoint/*",
    ]
    assert "A2AInvokePeerRuntimes" not in _sids(spec)  # that one is for A2A *servers*
    assert "A2AInvokeMountedPeers" not in _sids(
        AgentSpec(name="plain", method="harness", system_prompt="p"))
    # partition-neutral: the ARN is the mounted target's own, never rebuilt from a literal
    assert agent_iam.policy_document(spec, CTX)["Version"] == "2012-10-17"


def test_ask_tool_sends_runtime_user_id_from_the_bound_actor(consumer_module):
    """N10v3-bug5 live correction: without runtimeUserId the PEER's Runtime injects no
    workload token, and a runtime-linked identity cannot self-mint one — so every peer
    invoke carries the consumer's actor (else its session), bounded to 1024 chars."""
    module, plane = consumer_module
    module.a2a_bind_session("console-session-1", "river")
    module.ask_aurora_faq_a2a("hi")
    assert plane.calls[-1]["runtimeUserId"] == "river"
    module.a2a_bind_session("console-session-1", "")  # no actor → the consumer session
    module.ask_aurora_faq_a2a("hi")
    assert plane.calls[-1]["runtimeUserId"] == "console-session-1"
    module.a2a_bind_session("console-session-1", "x" * 3000)
    module.ask_aurora_faq_a2a("hi")
    assert plane.calls[-1]["runtimeUserId"] == "x" * 1024
    # the Strands entrypoint binds both per turn (the harness falls back to the session)
    source = render_main_py(_spec(method="zip_runtime"))
    assert "a2a_bind_session(session_id, actor_id)" in source
