"""Per-agent memory scoping.

AgentCore Memory keys both long-term namespaces (``/facts/{actorId}``,
``/preferences/{actorId}``) and short-term events only on ``actorId`` (plus
``sessionId``) — there is no ``{agentId}`` template variable and all agents
share one memory resource. Folding the agent id into the actor is therefore the
single lever that partitions BOTH stores per agent, so one agent's learned
facts never bleed into another's for the same human. These tests pin that the
lever is applied consistently on every write and read boundary.
"""

import app.routers.chat as chat_router
import app.services.memory as memory_service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, ChatSession
from app.services.memory import scoped_actor

from .conftest import ws_ctx


def make_active_agent(name="mem-agent") -> str:
    db = SessionLocal()
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name=name, method="zip_runtime", status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/x", spec={"name": name},
    )
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    return agent_id


def test_scoped_actor_folds_agent_id():
    assert scoped_actor("a1b2", "river") == "a1b2__river"
    assert scoped_actor("a1b2") == "a1b2__river"  # default human actor
    # distinct agents → distinct partitions for the same human
    assert scoped_actor("agentA", "river") != scoped_actor("agentB", "river")
    # uuid4-hex agent ids keep the compound id namespace-path-safe
    compound = scoped_actor("0123456789abcdef0123456789abcdef", "river")
    assert "/" not in compound and " " not in compound


def test_chat_write_passes_scoped_actor_but_ledger_keeps_human(client, monkeypatch):
    agent_id = make_active_agent(name="mem-write")
    captured: dict[str, str] = {}

    def fake_stream(agent, prompt, session_id=None, actor_id="river", **_kw):
        captured["actor_id"] = actor_id
        yield {"event": "meta",
               "data": {"session_id": "s" * 40, "agent": agent.name, "mode": "buffered"}}
        yield {"event": "done", "data": {"latency_ms": 1}}

    monkeypatch.setattr(chat_router, "chat_stream", fake_stream)
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi"})
    assert res.status_code == 200
    # the invoke chain (→ runtime write + long-term extraction) sees the scoped actor
    assert captured["actor_id"] == f"{agent_id}__river"
    # but the sessions ledger records the bare human actor for display
    db = SessionLocal()
    row = db.query(ChatSession).filter(ChatSession.agent_id == agent_id).first()
    db.close()
    assert row is not None and row.actor_id == "river"


def test_existing_session_keeps_original_actor_partition(client, monkeypatch):
    agent_id = make_active_agent(name="mem-existing")
    session_id = "s" * 40
    db = SessionLocal()
    db.add(
        ChatSession(
            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id=agent_id,
            session_id=session_id,
            actor_id="runtime-diagnostic",
        )
    )
    db.commit()
    db.close()
    captured: dict[str, str] = {}

    def fake_stream(agent, prompt, session_id=None, actor_id="river", **_kw):
        captured["actor_id"] = actor_id
        yield {
            "event": "meta",
            "data": {"session_id": session_id, "agent": agent.name, "mode": "buffered"},
        }
        yield {"event": "done", "data": {"latency_ms": 1}}

    monkeypatch.setattr(chat_router, "chat_stream", fake_stream)
    response = client.post(
        f"/api/chat/{agent_id}",
        json={"prompt": "continue", "session_id": session_id, "actor_id": "river"},
    )

    assert response.status_code == 200
    assert captured["actor_id"] == f"{agent_id}__runtime-diagnostic"

    summary_actor: dict[str, str] = {}

    def fake_summary(_ws, actor_id, requested_session_id):
        summary_actor["actor_id"] = actor_id
        assert requested_session_id == session_id
        return {"event_count": 0, "events": [], "records": []}

    monkeypatch.setattr(memory_service, "session_memory_summary", fake_summary)
    memory_response = client.get(
        f"/api/chat/{agent_id}/memory",
        params={"session_id": session_id, "actor_id": "river"},
    )
    assert memory_response.status_code == 200
    assert summary_actor["actor_id"] == f"{agent_id}__runtime-diagnostic"


def test_session_memory_read_uses_same_scoped_actor(client, monkeypatch):
    agent_id = make_active_agent(name="mem-read")
    captured: dict[str, str] = {}

    def fake_summary(_ws, actor_id, session_id):
        captured["actor_id"] = actor_id
        return {"event_count": 0, "events": [], "records": []}

    monkeypatch.setattr(memory_service, "session_memory_summary", fake_summary)
    res = client.get(f"/api/chat/{agent_id}/memory", params={"session_id": "s" * 40})
    assert res.status_code == 200
    # read partition must equal the write partition, else the rail shows nothing
    assert captured["actor_id"] == f"{agent_id}__river"


def test_summary_echoes_the_partition_it_read(client, monkeypatch):
    """The rail deep-links into the Memory console, which keys on the compound
    actor. Echo the id the read actually used instead of letting the frontend
    re-derive it — a session may have recorded a different human actor."""
    agent_id = make_active_agent(name="mem-link")
    session_id = "s" * 40
    db = SessionLocal()
    db.add(
        ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                    session_id=session_id, actor_id="runtime-diagnostic")
    )
    db.commit()
    db.close()

    monkeypatch.setattr(
        memory_service,
        "session_memory_summary",
        lambda _ws, actor_id, sid: {"event_count": 0, "events": [], "records": []},
    )
    body = client.get(
        f"/api/chat/{agent_id}/memory",
        params={"session_id": session_id, "actor_id": "river"},
    ).json()

    # not `<agent>__river` — the recorded actor wins, and the link must follow it
    assert body["actor_id"] == f"{agent_id}__runtime-diagnostic"


def test_summary_display_label_hides_compound_actor(monkeypatch):
    """The rail chip shows the strategy (/facts, /preferences), never the
    compound actor id."""
    monkeypatch.setattr(memory_service, "list_events", lambda *a, **k: [])
    monkeypatch.setattr(
        memory_service, "list_records",
        lambda _ws, ns, max_results=10: [{"content": {"text": "likes brevity"},
                                          "memoryRecordId": "r1"}],
    )
    out = memory_service.session_memory_summary(ws_ctx(), "agentX__river", "sess")
    labels = {r["namespace"] for r in out["records"]}
    assert labels <= {"/preferences", "/facts"}
    assert all("river" not in r["namespace"] for r in out["records"])


def test_rail_renders_structured_preference_records_readably(monkeypatch):
    """/preferences records are stored as JSON objects; the rail must show the
    sentence, not the serialized object."""
    stored = (
        '{"context":"The user said so.","preference":"Wants numbered lists",'
        '"categories":["formatting"]}'
    )
    monkeypatch.setattr(memory_service, "list_events", lambda *a, **k: [])
    monkeypatch.setattr(
        memory_service, "list_records",
        lambda _ws, ns, max_results=10: [
            {"content": {"text": stored}, "memoryRecordId": "r1"}
        ],
    )
    out = memory_service.session_memory_summary(ws_ctx(), "agentX__river", "sess")
    assert all(r["text"] == "Wants numbered lists" for r in out["records"])
