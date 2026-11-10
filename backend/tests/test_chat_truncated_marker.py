"""N-SCN-bug4 follow-up (r8-a): the runtime's ``truncated: true`` marker reaches the console.

Hermetic: ``invoke_agent_events`` / ``invoke_agent_text`` are stubbed; no AWS. Covers the
three places the marker now lives — the chat ledger row (``ChatMessage.truncated``), the
``/history`` replay (additive ``truncated: true`` on the agent row) and the observability
transcript turn (``truncated: true`` on the ASSISTANT turn, from the ledger whether the
transcript came from Memory or from the ledger) — plus the buffered invoke path, which now
forwards the marker as its own ``truncated`` event like the native stream does.

r8-a2: both ``/v1`` entrances (sync ``invoke`` through ``chat_ledger.record_turn``, SSE
``invoke-stream`` through ``chat_ledger.record_stream``) persist the same flag, so a
public-API session replayed in the console carries the notice as well.
"""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, ChatMessage, ChatSession
from app.services import chat as chat_service
from app.services import invoke as invoke_service
from app.services import memory_backend
from app.services import observability as obs
from tests.test_chat_api import make_active_agent
from tests.test_observability import _FakeMemoryBackend, _seed_agent, ws_ctx


@pytest.fixture
def fake_memory(client):
    """The transcript's Memory read — registered after ``create_app()`` (see the
    autouse fixture in ``test_observability.py`` for why the order matters)."""
    fake = _FakeMemoryBackend()
    memory_backend.set_backend(fake)
    yield fake
    memory_backend.set_backend(None)

NOTICE = "[response truncated at the model's token limit]"


def _events(*events):
    return lambda *args, **kwargs: iter(list(events))


def test_truncated_marker_is_persisted_on_the_bubble_and_replayed(client, monkeypatch):
    agent_id = make_active_agent(method="container", name="chat-trunc-agent")
    monkeypatch.setattr(chat_service, "invoke_agent_events", _events(
        {"event": "delta", "data": {"text": "partial answer "}},
        {"event": "delta", "data": {"text": NOTICE}},
        {"event": "truncated", "data": {"truncated": True, "stop_reason": "max_tokens",
                                        "continuations": 1}},
        {"event": "complete", "data": {"text": "partial answer " + NOTICE, "truncated": True}},
    ))
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "compute"})
    assert res.status_code == 200
    assert 'event: truncated\ndata: {"truncated": true' in res.text  # forwarded unchanged
    sid = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"][0]["session_id"]
    history = client.get(f"/api/chat/{agent_id}/history",
                         params={"session_id": sid}).json()["messages"]
    assert [(m["role"], m.get("truncated")) for m in history] == [
        ("user", None), ("agent", True)]
    assert history[1]["text"].endswith(NOTICE)
    db = SessionLocal()
    rows = db.query(ChatMessage).filter(ChatMessage.session_id == sid).all()
    db.close()
    assert [(r.role, r.truncated) for r in rows] == [("user", False), ("agent", True)]


def test_truncated_false_marker_does_not_flag_the_continued_bubble(client, monkeypatch):
    """``truncated: false`` separates the partial text from its continuation — the turn
    finished, so the bubble is an ordinary answer."""
    agent_id = make_active_agent(method="container", name="chat-cont-agent")
    monkeypatch.setattr(chat_service, "invoke_agent_events", _events(
        {"event": "delta", "data": {"text": "part one "}},
        {"event": "truncated", "data": {"truncated": False, "stop_reason": "max_tokens",
                                        "continuations": 1}},
        {"event": "delta", "data": {"text": "part two"}},
        {"event": "complete", "data": {"text": "part one part two"}},
    ))
    client.post(f"/api/chat/{agent_id}", json={"prompt": "go"})
    sid = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"][0]["session_id"]
    history = client.get(f"/api/chat/{agent_id}/history",
                         params={"session_id": sid}).json()["messages"]
    assert history[1]["text"] == "part one part two"
    assert "truncated" not in history[1]  # additive: absent on a complete answer


def test_tool_call_scopes_the_marker_to_the_bubble_it_closed(client, monkeypatch):
    """A tool call splits the answer; only the bubble the marker arrived in is flagged."""
    agent_id = make_active_agent(method="container", name="chat-trunc-tool-agent")
    monkeypatch.setattr(chat_service, "invoke_agent_events", _events(
        {"event": "delta", "data": {"text": "thinking " + NOTICE}},
        {"event": "truncated", "data": {"truncated": True}},
        {"event": "tool", "data": {"name": "calc", "id": "t1"}},
        {"event": "delta", "data": {"text": "final"}},
    ))
    client.post(f"/api/chat/{agent_id}", json={"prompt": "go"})
    sid = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"][0]["session_id"]
    history = client.get(f"/api/chat/{agent_id}/history",
                         params={"session_id": sid}).json()["messages"]
    assert [(m["role"], m.get("truncated")) for m in history] == [
        ("user", None), ("agent", True), ("tool", None), ("agent", None)]


def test_buffered_invoke_path_forwards_the_marker_as_an_event(monkeypatch):
    db = SessionLocal()
    agent = db.get(Agent, make_active_agent(method="studio", name="buffered-trunc-agent"))
    db.close()
    monkeypatch.setattr(invoke_service, "invoke_agent_text", lambda *a, **k: {
        "text": "x" * 70 + NOTICE, "session_id": "s" * 40, "truncated": True})
    events = list(invoke_service.invoke_agent_events(agent, "hi", workspace=ws_ctx()))
    kinds = [e["event"] for e in events]
    assert kinds[:2] == ["delta", "delta"] and "truncated" in kinds
    marker = next(e for e in events if e["event"] == "truncated")
    assert marker["data"] == {"truncated": True, "stop_reason": "max_tokens", "continuations": 0}
    assert kinds.index("truncated") > kinds.index("delta")  # after the text, like the stream

    monkeypatch.setattr(invoke_service, "invoke_agent_text", lambda *a, **k: {
        "text": "done", "session_id": "s" * 40})
    assert "truncated" not in [
        e["event"] for e in invoke_service.invoke_agent_events(agent, "hi", workspace=ws_ctx())]


# --- /v1: the shared ledger (services/chat_ledger.py) carries the flag too (r8-a2) ------


def _mint_key(client) -> str:
    return client.post("/api/apikeys", json={"name": "v1-trunc"}).json()["key"]


def test_v1_sync_invoke_persists_the_truncated_flag_via_record_turn(client, monkeypatch):
    import app.routers.public_api as public_api

    agent_id = make_active_agent(name="v1-trunc-sync-agent")
    results = iter([
        {"text": "cut " + NOTICE, "session_id": "v1" + "t" * 31, "truncated": True},
        {"text": "whole answer", "session_id": "v1" + "t" * 31},
    ])
    monkeypatch.setattr(public_api, "invoke_agent_text", lambda *a, **k: next(results))
    key = _mint_key(client)
    res = client.post(f"/v1/agents/{agent_id}/invoke", json={"prompt": "q1"},
                      headers={"X-Api-Key": key})
    assert res.status_code == 200 and res.json()["truncated"] is True
    sid = res.json()["session_id"]
    res = client.post(f"/v1/agents/{agent_id}/invoke", json={"prompt": "q2", "session_id": sid},
                      headers={"X-Api-Key": key})
    assert res.status_code == 200 and "truncated" not in res.json()

    history = client.get(f"/api/chat/{agent_id}/history",
                         params={"session_id": sid}).json()["messages"]
    assert [(m["role"], m.get("truncated")) for m in history] == [
        ("user", None), ("agent", True), ("user", None), ("agent", None)]
    sessions = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"]
    assert sessions[0]["source"] == "public_api"  # the entrance marker is unchanged


@pytest.mark.parametrize("marker", [
    pytest.param({"event": "truncated", "data": {"truncated": True, "stop_reason": "max_tokens",
                                                 "continuations": 1}}, id="truncated-event"),
    pytest.param({"event": "complete", "data": {"text": "x", "truncated": True}},
                 id="complete-marker"),
])
def test_v1_stream_persists_the_truncated_flag_per_bubble_via_record_stream(
    client, monkeypatch, marker,
):
    """Same bubble semantics as the console router: the marker flags the bubble it
    arrived in, a tool call closes that bubble first, the continuation is plain."""
    agent_id = make_active_agent(name="v1-trunc-stream-agent")
    monkeypatch.setattr(chat_service, "invoke_agent_events", _events(
        {"event": "delta", "data": {"text": "part one " + NOTICE}},
        marker,
        {"event": "tool", "data": {"name": "calc", "id": "t1"}},
        {"event": "delta", "data": {"text": "part two"}},
    ))
    res = client.post(f"/v1/agents/{agent_id}/invoke-stream", json={"prompt": "stream"},
                      headers={"X-Api-Key": _mint_key(client)})
    assert res.status_code == 200, res.text
    assert f"event: {marker['event']}" in res.text  # forwarded unchanged
    sid = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"][0]["session_id"]
    history = client.get(f"/api/chat/{agent_id}/history",
                         params={"session_id": sid}).json()["messages"]
    assert [(m["role"], m["text"], m.get("truncated")) for m in history] == [
        ("user", "stream", None), ("agent", "part one " + NOTICE, True),
        ("tool", "", None), ("agent", "part two", None)]


def test_v1_stream_truncated_false_marker_is_not_a_flag(client, monkeypatch):
    agent_id = make_active_agent(name="v1-cont-stream-agent")
    monkeypatch.setattr(chat_service, "invoke_agent_events", _events(
        {"event": "delta", "data": {"text": "part one "}},
        {"event": "truncated", "data": {"truncated": False, "continuations": 1}},
        {"event": "delta", "data": {"text": "part two"}},
    ))
    client.post(f"/v1/agents/{agent_id}/invoke-stream", json={"prompt": "go"},
                headers={"X-Api-Key": _mint_key(client)})
    sid = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"][0]["session_id"]
    history = client.get(f"/api/chat/{agent_id}/history",
                         params={"session_id": sid}).json()["messages"]
    assert history[1]["text"] == "part one part two" and "truncated" not in history[1]


def _seed_chat(agent_id: str, session_id: str) -> None:
    db = SessionLocal()
    db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                       session_id=session_id, actor_id="river"))
    db.add_all([
        ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                    session_id=session_id, role="user", text="q1"),
        ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                    session_id=session_id, role="agent", text="a1 " + NOTICE, truncated=True),
        ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                    session_id=session_id, role="user", text="q2"),
        ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                    session_id=session_id, role="agent", text="a2"),
    ])
    db.commit()
    db.close()


def _memory_events(*pairs):
    return [{"eventTimestamp": "2026-07-10T01:00:00", "payload": [
        {"conversational": {"role": role, "content": {"text": text}}} for role, text in pairs]}]


def test_transcript_turn_carries_the_marker_when_memory_matches_the_ledger(fake_memory):
    agent_id = _seed_agent(name="trunc-mem-agent", resource_id="trunc_mem")
    session_id = "m" * 64
    _seed_chat(agent_id, session_id)
    fake_memory.events = lambda *a, **k: _memory_events(
        ("USER", "q1"), ("ASSISTANT", "a1 " + NOTICE), ("USER", "q2"), ("ASSISTANT", "a2"))
    db = SessionLocal()
    result = obs.session_transcript(db, session_id, ws_ctx())
    db.close()
    assert result["origin"] == "memory"
    assert [t.get("truncated") for t in result["turns"]] == [None, True, None, None]


def test_transcript_turn_carries_the_marker_when_the_ledger_wins(fake_memory):
    agent_id = _seed_agent(name="trunc-ledger-agent", resource_id="trunc_ledger")
    session_id = "l" * 64
    _seed_chat(agent_id, session_id)
    fake_memory.events = lambda *a, **k: _memory_events(("USER", "q1"))
    db = SessionLocal()
    result = obs.session_transcript(db, session_id, ws_ctx())
    db.close()
    assert result["origin"] == "ledger"
    assert [(t["role"], t.get("truncated")) for t in result["turns"]] == [
        ("USER", None), ("ASSISTANT", True), ("USER", None), ("ASSISTANT", None)]
