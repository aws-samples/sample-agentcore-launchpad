"""Front-desk A2A demo — endpoint trace passthrough + sample pure helpers."""

import json
from contextvars import ContextVar
from pathlib import Path

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "frontdesk_agent" / "main.py"


def _mk_agent(**kw) -> str:
    db = SessionLocal()
    agent = Agent(**{
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "name": "front-desk", "method": "zip_runtime", "status": "active",
        "arn": "arn:aws:bedrock-agentcore:us-west-2:1:runtime/fd-1",
        "spec": {"system_prompt": "s"}, **kw,
    })
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    return agent_id


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


def test_a2a_demo_passes_trace_through(client, monkeypatch):
    from app.services.agentcore import client as ac_client

    trace = [{"stage": "discover", "query": "refunds", "hits": []}]
    stub = _DataStub({"result": "[via aurora-faq-a2a] 30 days.", "a2a_trace": trace})
    monkeypatch.setattr(ac_client, "data_client", lambda: stub)
    # the router imports data_client inside the handler → patch the source module
    agent_id = _mk_agent()
    res = client.post("/api/registry/a2a-demo",
                      json={"agent_id": agent_id, "question": "refund policy?"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["answer"].startswith("[via aurora-faq-a2a]")
    assert body["trace"] == trace
    payload = json.loads(stub.invoked_with["payload"])
    assert payload["prompt"] == "refund policy?"


def test_a2a_demo_rejects_missing_or_harness_agents(client):
    res = client.post("/api/registry/a2a-demo",
                      json={"agent_id": "nope", "question": "q"})
    assert res.status_code == 404
    harness_id = _mk_agent(name="front-harness", method="harness",
                           arn="arn:aws:bedrock-agentcore:us-west-2:1:harness/h-1")
    res = client.post("/api/registry/a2a-demo",
                      json={"agent_id": harness_id, "question": "q"})
    assert res.status_code == 400
    assert res.json()["code"] == "registry.a2a_demo_unsupported"


def test_a2a_demo_refuses_an_agent_from_another_workspace(client):
    """This is the second invoke entrance, so it owes the same workspace boundary
    as /api/agents/{id}/invoke: a foreign id reads as missing."""
    foreign = _mk_agent(name="foreign-desk", workspace_id="acct-usw1")
    res = client.post("/api/registry/a2a-demo",
                      json={"agent_id": foreign, "question": "q"})
    assert res.status_code == 404
    assert res.json()["code"] == "registry.a2a_demo_agent"


# ─── sample pure helpers (imported without strands/bedrock deps) ─────────────
def _load_helpers():
    """Extract the pure functions from the sample without importing strands."""
    src = SAMPLE.read_text(encoding="utf-8")
    module: dict = {}
    # execute only up to the first @tool (pure section); stub the imports
    pure_src = src.split("@tool", 1)[0]
    pure_src = pure_src.replace("import boto3\n", "")
    pure_src = pure_src.replace(
        "from bedrock_agentcore.runtime import BedrockAgentCoreApp\n", "")
    pure_src = pure_src.replace("from strands import Agent, tool\n", "")
    exec(compile(pure_src, str(SAMPLE), "exec"), module)  # noqa: S102 — test fixture
    return module


def test_sample_compiles():
    compile(SAMPLE.read_text(encoding="utf-8"), str(SAMPLE), "exec")


def test_arn_from_url_variants():
    h = _load_helpers()
    enc = ("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
           "arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A1%3Aruntime%2Fx-1/invocations/")
    assert h["arn_from_url"](enc) == "arn:aws:bedrock-agentcore:us-west-2:1:runtime/x-1"
    assert h["arn_from_url"]("arn:aws:bedrock-agentcore:us-west-2:1:harness/h-1") \
        == "arn:aws:bedrock-agentcore:us-west-2:1:harness/h-1"
    assert h["arn_from_url"]("https://nope.example/") == ""


def test_derive_session_stable_per_agent_random_when_adhoc():
    h = _load_helpers()
    fd = "83e9260598e14c3c96ba73fb802800f0e072149db400408eabce708c5150164a"
    a = h["derive_session"](fd, "aurora-support")
    assert a == h["derive_session"](fd, "aurora-support")  # stable
    assert a != h["derive_session"](fd, "hr-bot")          # per-agent
    assert a != h["derive_session"]("other-session", "aurora-support")
    assert len(a) == 64 and all(c in "0123456789abcdef" for c in a)
    # adhoc: no front-desk session → random, still a valid runtimeSessionId
    r1, r2 = h["derive_session"]("", "aurora-support"), h["derive_session"]("", "aurora-support")
    assert r1 != r2 and len(r1) >= 16


def test_parse_card_and_a2a_reply_text():
    h = _load_helpers()
    card = {"name": "aurora-faq-a2a", "description": "d", "url": "u",
            "skills": [{"name": "faq", "description": "x", "tags": ["support"]}],
            "metadata": {"launchpad.transport": "a2a-jsonrpc",
                         "launchpad.method": "zip_runtime"}}
    rec = {
        "name": "aurora-faq-a2a",
        "descriptors": {"a2aAgentCard": {"data": json.dumps(card)}},
    }
    parsed = h["parse_card"](rec)
    assert parsed["transport"] == "a2a-jsonrpc"
    assert parsed["skills"][0]["tags"] == ["support"]
    assert h["parse_card"]({"descriptors": {}}) is None
    # Task shape wins over history; Message shape parses parts directly
    task = {"artifacts": [{"parts": [{"kind": "text", "text": "ok"}]}],
            "history": [{"parts": [{"kind": "text", "text": "junk"}]}]}
    assert h["_a2a_reply_text"](task) == "ok"
    assert h["_a2a_reply_text"]({"kind": "message",
                                 "parts": [{"kind": "text", "text": "hi"}]}) == "hi"


def test_frontdesk_a2a_message_carries_context_and_request_state_is_isolated():
    h = _load_helpers()
    payload = h["a2a_message"]("remember this", "session-123")
    message = payload["params"]["message"]
    assert message["contextId"] == "session-123"
    assert message["parts"] == [{"kind": "text", "text": "remember this"}]
    assert isinstance(h["_TRACE"], ContextVar)
    assert isinstance(h["_LAST_HITS"], ContextVar)
    assert isinstance(h["_SESSION_ID"], ContextVar)


def test_frontdesk_memory_manager_scopes_actor_and_session():
    h = _load_helpers()
    captured = {}

    class FakeManager:
        def __init__(self, config, region_name=None):
            captured["config"] = config
            captured["region_name"] = region_name

    h["MEMORY_ID"] = "memory-123"
    h["AgentCoreMemorySessionManager"] = FakeManager
    manager = h["memory_session_manager"]("frontdesk__river", "session-123")

    assert isinstance(manager, FakeManager)
    assert captured["config"].memory_id == "memory-123"
    assert captured["config"].actor_id == "frontdesk__river"
    assert captured["config"].session_id == "session-123"
    assert captured["region_name"] == "us-west-2"
