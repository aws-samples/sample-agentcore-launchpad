"""N16-obs78: request bodies the console builds refuse unknown keys (422
``validation.invalid_request`` naming the key), while specs READ BACK from the ledger stay
tolerant — an old row may carry a key a later build retired."""

import pytest
from pydantic import ValidationError

import app.routers.agents as agents_router
from app.schemas.agent import AgentSpec, AgentSpecRequest
from tests.test_chat_session_stop import make_agent

SPEC = {"name": "extra-check", "method": "zip_runtime", "system_prompt": "You help."}


@pytest.fixture
def no_real_deploy(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    return launched


def _extra_keys(body: dict) -> list[str]:
    return [e["loc"][-1] for e in body["detail"] if e["type"] == "extra_forbidden"]


def test_create_agent_refuses_an_unknown_top_level_key(client, no_real_deploy):
    res = client.post("/api/agents", json={**SPEC, "frobnicate": 1})
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "validation.invalid_request"
    assert _extra_keys(body) == ["frobnicate"]
    assert no_real_deploy == []  # nothing was created
    assert client.get("/api/agents").json()["agents"] == []


def test_redeploy_refuses_an_unknown_top_level_key(client, no_real_deploy):
    created = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    res = client.post(f"/api/agents/{created}/redeploy", json={**SPEC, "frobnicate": 1})
    assert res.status_code == 422, res.text
    assert _extra_keys(res.json()) == ["frobnicate"]


def test_creator_fields_keep_their_own_message(client, no_real_deploy):
    """The server-set creator refusal (Family B, a before-validator) still runs first, so
    its explanation is what the client reads — not a bare ``extra_forbidden``."""
    res = client.post("/api/agents", json={**SPEC, "created_by": "u1"})
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "validation.invalid_request"
    assert any("creator is recorded by the server" in e["msg"] for e in body["detail"])
    assert _extra_keys(body) == []


def test_chat_request_refuses_an_unknown_key(client):
    agent_id = make_agent(name="extra-chat-agent")
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi", "created_by": "admin"})
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "validation.invalid_request"
    assert _extra_keys(body) == ["created_by"]
    assert client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"] == []


def test_stored_spec_with_a_retired_key_still_loads():
    """The ledger boundary: ``AgentSpec(**row.spec)`` everywhere in the deployer / invoke
    path must keep parsing a row written by an earlier build."""
    stored = {**SPEC, "legacy_retired_key": {"anything": True}}
    spec = AgentSpec(**stored)
    assert spec.name == "extra-check"
    assert "legacy_retired_key" not in spec.model_dump()
    with pytest.raises(ValidationError, match="legacy_retired_key"):
        AgentSpecRequest(**stored)


def test_a_stored_spec_dump_round_trips_as_a_request():
    """The architect proposal path dumps an ``AgentSpec`` the console posts back verbatim:
    every dumped key must be a field the request body accepts (harness and A2A shapes)."""
    harness = AgentSpec(name="proposal", method="harness", system_prompt="x",
                        tools=[{"type": "builtin", "name": "code-interpreter"}],
                        knowledge_bases=[{"kb_id": "kb1", "name": "n", "description": "d"}],
                        max_iterations=5, timeout_seconds=60)
    a2a = AgentSpec(name="peer", method="zip_runtime", system_prompt="x", protocol="a2a",
                    a2a_skills=[{"id": "faq", "name": "FAQ", "description": "d", "tags": []}])
    for spec in (harness, a2a):
        again = AgentSpecRequest.model_validate(spec.model_dump())
        assert again.model_dump() == spec.model_dump()
