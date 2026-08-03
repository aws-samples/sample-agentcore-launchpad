"""A2A-protocol agents — spec validation, template, deploy params, invoke,
card enrichment, experiment gating."""

import json

import pytest
from pydantic import ValidationError

from app.core.db import SessionLocal
from app.deployer.zip_runtime import _generate_code, _method_requirements
from app.models.ledger import Agent
from app.schemas.agent import A2ASkill, AgentSpec
from app.services.agentcore import registry as reg
from app.services.agentcore import runtime as rt

SKILL = {"id": "faq", "name": "Product FAQ",
         "description": "Answers product questions", "tags": ["support"]}


def _spec(**kw) -> AgentSpec:
    base = {"name": "a2a-check", "method": "zip_runtime", "system_prompt": "You help."}
    return AgentSpec(**{**base, **kw})


# ─── spec validation ─────────────────────────────────────────────────────────
def test_protocol_defaults_to_http_and_old_specs_revalidate():
    assert _spec().protocol == "http"
    assert _spec().a2a_skills == []


def test_a2a_requires_zip_runtime_method():
    for method in ("harness", "container"):
        with pytest.raises(ValidationError, match="zip_runtime"):
            _spec(method=method, protocol="a2a")
    assert _spec(protocol="a2a").protocol == "a2a"


def test_a2a_rejects_custom_code_and_bundle():
    with pytest.raises(ValidationError, match="A2A template"):
        _spec(protocol="a2a", code="print('x')")
    with pytest.raises(ValidationError, match="A2A template"):
        _spec(protocol="a2a", code_bundle={"main.py": "print('x')"})


def test_a2a_skills_need_a2a_protocol_and_unique_ids():
    with pytest.raises(ValidationError, match="require protocol=a2a"):
        _spec(a2a_skills=[A2ASkill(**SKILL)])
    with pytest.raises(ValidationError, match="unique"):
        _spec(protocol="a2a", a2a_skills=[A2ASkill(**SKILL), A2ASkill(**SKILL)])


# ─── template + generate dispatch ────────────────────────────────────────────
def test_a2a_template_renders_compiles_and_carries_skills():
    spec = _spec(protocol="a2a", a2a_skills=[A2ASkill(**SKILL)])
    code, source = _generate_code(spec)
    assert source == "strands A2A template"
    compile(code, "main.py", "exec")  # brace-safe contract
    assert "A2AServer(" in code and "serve_at_root=True" in code
    assert "port=9000" in code
    assert "'id': 'faq'" in code
    assert "AGENTCORE_RUNTIME_URL" in code
    assert "AgentCoreMemorySessionManager" in code
    assert 'actor_id=f"{AGENT_NAME}__a2a__{context_id}"' in code


def test_http_template_untouched_by_protocol_field():
    code, source = _generate_code(_spec())
    assert source == "strands template"
    assert "A2AServer" not in code


def test_a2a_requirements_carry_the_a2a_extra():
    reqs = _method_requirements(_spec(protocol="a2a", requirements=["httpx==0.28.1"]))
    assert any(r.startswith("strands-agents[a2a") for r in reqs)
    assert "httpx==0.28.1" in reqs
    http_reqs = _method_requirements(_spec())
    assert not any("[a2a" in r for r in http_reqs)


def test_a2a_ignores_model_source_without_breaking():
    """A2A is Converse-only (see strands_a2a_agent/__init__.py). A mantle spec must
    still render and compile — the wizard is what keeps A2A off Mantle."""
    spec = _spec(protocol="a2a", model_source="mantle", model_id="openai.gpt-5.6-sol")
    code, _ = _generate_code(spec)
    compile(code, "main.py", "exec")
    assert 'MODEL_ID = "openai.gpt-5.6-sol"' in code
    assert "OpenAIResponsesModel" not in code
    # and the openai extra is not dragged into the vendored a2a wheel set
    assert not any("[openai]" in r for r in _method_requirements(spec))


# ─── deploy params ───────────────────────────────────────────────────────────
class _Stub:
    def __init__(self):
        self.created_with = None
        self.updated_with = None

    def create_agent_runtime(self, **kw):
        self.created_with = kw
        return {"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt-1"}

    def update_agent_runtime(self, **kw):
        self.updated_with = kw
        return {"agentRuntimeId": kw["agentRuntimeId"], "agentRuntimeVersion": "2"}


def test_create_code_runtime_sets_protocol_only_for_a2a():
    stub = _Stub()
    rt.create_code_runtime(stub, runtime_name="n", s3_bucket="b", s3_key="k",
                           role_arn="r", protocol="a2a")
    assert stub.created_with["protocolConfiguration"] == {"serverProtocol": "A2A"}
    stub2 = _Stub()
    rt.create_code_runtime(stub2, runtime_name="n", s3_bucket="b", s3_key="k",
                           role_arn="r", protocol="http")
    assert "protocolConfiguration" not in stub2.created_with


def test_update_code_runtime_echoes_protocol():
    """UpdateAgentRuntime resets an omitted protocolConfiguration (probed live)
    — the update path must always re-send it for A2A agents."""
    stub = _Stub()
    rt.update_code_runtime(stub, runtime_id="rt-1", s3_bucket="b", s3_key="k",
                           role_arn="r", protocol="a2a")
    assert stub.updated_with["protocolConfiguration"] == {"serverProtocol": "A2A"}


# ─── invoke ──────────────────────────────────────────────────────────────────
class _DataStub:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()
        self.invoked_with = None

    def invoke_agent_runtime(self, **kw):
        self.invoked_with = kw

        class R:
            def __init__(self, d):
                self._d = d

            def read(self):
                return self._d

        return {"response": R(self.body)}


def test_invoke_a2a_text_sends_message_send_and_parses_task():
    stub = _DataStub({"jsonrpc": "2.0", "id": "1", "result": {
        "artifacts": [{"parts": [{"kind": "text", "text": "A2A OK"}]}],
        # history carries the user turn + agent streaming fragments (probed
        # live) — must be ignored entirely. The user turn is load-bearing:
        # fragments alone join to exactly "A2A OK", so a parser that wrongly
        # read history instead of artifacts would still pass without it.
        "history": [{"role": "user", "parts": [{"kind": "text", "text": "ping"}]},
                    {"role": "agent", "parts": [{"kind": "text", "text": "A2"}]},
                    {"role": "agent", "parts": [{"kind": "text", "text": "A OK"}]}],
    }})
    session_id = "s" * 40
    out = rt.invoke_a2a_text(stub, "arn:rt-1", "ping", session_id=session_id)
    assert out["text"] == "A2A OK"
    payload = json.loads(stub.invoked_with["payload"])
    assert payload["method"] == "message/send"
    assert payload["params"]["message"]["parts"] == [{"kind": "text", "text": "ping"}]
    assert payload["params"]["message"]["contextId"] == session_id
    assert stub.invoked_with["runtimeSessionId"] == session_id
    assert len(stub.invoked_with["runtimeSessionId"]) >= 16


def test_invoke_a2a_text_parses_message_shape():
    stub = _DataStub({"result": {"kind": "message",
                                 "parts": [{"kind": "text", "text": "hi"}]}})
    assert rt.invoke_a2a_text(stub, "arn:rt-1", "x")["text"] == "hi"


def test_invoke_a2a_text_raises_jsonrpc_error():
    stub = _DataStub({"error": {"code": -32502, "message": "Validation error"}})
    with pytest.raises(RuntimeError, match="-32502"):
        rt.invoke_a2a_text(stub, "arn:rt-1", "x")


def test_invoke_agent_text_dispatches_on_protocol(monkeypatch):
    from app.services import invoke as inv

    calls = []
    monkeypatch.setattr(inv, "data_client", lambda: object())
    monkeypatch.setattr(inv.rt, "invoke_a2a_text",
                        lambda *a, **k: calls.append("a2a") or {"text": "", "session_id": "s"})
    monkeypatch.setattr(inv.rt, "invoke_runtime_text",
                        lambda *a, **k: calls.append("http") or {"text": "", "session_id": "s"})
    a2a_agent = Agent(name="x", method="zip_runtime", status="active",
                      arn="arn:rt", spec={"protocol": "a2a"})
    http_agent = Agent(name="y", method="zip_runtime", status="active",
                       arn="arn:rt", spec={})
    inv.invoke_agent_text(a2a_agent, "hi")
    inv.invoke_agent_text(http_agent, "hi")
    assert calls == ["a2a", "http"]


# ─── card enrichment ─────────────────────────────────────────────────────────
def test_data_plane_invocations_url_encodes_arn():
    url = reg.data_plane_invocations_url(
        "arn:aws:bedrock-agentcore:us-west-2:1:runtime/x-1", "us-west-2")
    assert url.startswith("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/")
    assert "arn%3Aaws" in url and url.endswith("/invocations/")


def test_build_a2a_card_defaults_match_legacy_shape():
    card = reg.build_a2a_card(name="n", description="d", arn="arn:x",
                              version="1", method="zip_runtime")
    assert card["url"] == "arn:x" and card["skills"] == []
    assert card["metadata"]["launchpad.transport"] == "agentcore-http"


def test_build_a2a_card_a2a_variant_carries_url_skills_transport():
    card = reg.build_a2a_card(
        name="n", description="d", arn="arn:x", version="1", method="zip_runtime",
        url="https://endpoint/invocations/", skills=[SKILL],
        transport="a2a-jsonrpc")
    assert card["url"] == "https://endpoint/invocations/"
    assert card["skills"] == [SKILL]
    assert card["metadata"]["launchpad.transport"] == "a2a-jsonrpc"
    assert "JSON-RPC" in card["metadata"]["launchpad.invoke"]


def test_derive_card_skills_explicit_wins():
    assert reg.derive_card_skills({"a2a_skills": [SKILL]}) == [SKILL]


def test_derive_card_skills_from_spec_surfaces():
    spec = {
        "method": "harness",
        "tools": [{"type": "builtin", "name": "code-interpreter"},
                  {"type": "gateway", "name": "aws-knowledge"}],
        "knowledge_bases": [{"kb_id": "BL6", "name": "aurora-deck-docs",
                             "description": "Aurora Deck product documentation"}],
        "skills": ["s3://bkt/skills/meeting-summarizer/"],
    }
    skills = reg.derive_card_skills(spec)
    assert [s["id"] for s in skills] == [
        "code-interpreter", "aws-knowledge", "aurora-deck-docs",
        "meeting-summarizer",
    ]
    kb = skills[2]
    assert kb["description"] == "Aurora Deck product documentation"  # routing signal
    assert kb["tags"] == ["knowledge"]


def test_derive_card_skills_zip_template_and_dedup():
    # template agents advertise the baked-in tools even with empty spec.tools
    assert [s["id"] for s in reg.derive_card_skills({"method": "zip_runtime"})] == [
        "calculator", "current-time",
    ]
    # code-carrying agents don't get template tools; duplicate names get suffixes
    studio = {"method": "studio", "code": "x", "tools": [
        {"type": "mcp", "name": "search"}, {"type": "gateway", "name": "Search"}]}
    assert [s["id"] for s in reg.derive_card_skills(studio)] == ["search", "search-2"]


# ─── eval run dispatch ───────────────────────────────────────────────────────
def test_execute_run_uses_a2a_invoke_for_a2a_protocol(monkeypatch):
    """The eval runner bypasses invoke_agent_text — its own dispatch must
    branch on protocol too (live run failed with JSON-RPC -32600 before)."""
    from app.evaluation import service as svc
    from app.evaluation.models import EvalRun
    from tests.evaluation.test_runs_flow import stub_environment

    stub_environment(monkeypatch)
    a2a_calls: list[str] = []
    http_calls: list[str] = []
    monkeypatch.setattr(
        svc.rt, "invoke_a2a_text",
        lambda client, arn, prompt, session_id=None:
        a2a_calls.append(prompt) or {"text": "ok", "session_id": "s" * 33})
    monkeypatch.setattr(
        svc.rt, "invoke_runtime_text",
        lambda *a, **k: http_calls.append("x") or {"text": "ok", "session_id": "s" * 33})

    db = SessionLocal()
    run = EvalRun(agent_id="a2a-x", agent_name="a2a-x", mode="evaluators",
                  evaluators=["Builtin.Correctness"], status="queued")
    db.add(run)
    db.commit()
    rid = run.id
    db.close()

    svc.execute_run(
        rid, agent_arn="arn:rt", method="zip_runtime", protocol="a2a",
        service_name="svc.DEFAULT", log_group="/lg",
        items=[{"prompt": "hello"}], evaluators=["Builtin.Correctness"],
        mode="evaluators", wait_seconds=0,
    )
    assert a2a_calls == ["hello"]
    assert http_calls == []


# ─── experiment gating ───────────────────────────────────────────────────────
def test_experiment_create_rejects_a2a_agents(client):
    db = SessionLocal()
    agent = Agent(name="a2a-agent", method="zip_runtime", status="active",
                  arn="arn:rt", resource_id="rt-9",
                  spec={"system_prompt": "s", "protocol": "a2a"})
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    res = client.post("/api/experiments", json={"agent_id": agent_id})
    assert res.status_code == 400
    assert res.json()["code"] == "experiment.protocol_unsupported"
