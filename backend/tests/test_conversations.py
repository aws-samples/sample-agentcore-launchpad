"""Studio local-debug conversations: history replay + failed-turn exclusion,
PUT code rewrite, multi-turn context, stream sentinels, endpoint wiring."""

import asyncio
import json
import os
import sys
import tempfile

# Isolate tests from data/launchpad.db BEFORE any app import binds the engine.
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="launchpad-test-"), "test.db")
os.environ["LAUNCHPAD_DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import DEFAULT_WORKSPACE_ID  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models.conversation import (  # noqa: E402
    ChatMessage,
    ConversationSession,
    CreateConversationRequest,
)
from app.services.conversation_service import ConversationService  # noqa: E402

# A dependency-free stand-in for generated agent code: parses the --messages
# replay, echoes turn count + last user text, and fails on the sentinel "fail".
ECHO_AGENT = (
    "import argparse, json, sys\n"
    "p = argparse.ArgumentParser()\n"
    "p.add_argument('--messages')\n"
    "p.add_argument('--user-input')\n"
    "a = p.parse_args()\n"
    "msgs = json.loads(a.messages) if a.messages else []\n"
    "users = [m for m in msgs if m['role'] == 'user']\n"
    "last = users[-1]['content'][0]['text'] if users else ''\n"
    "if last == 'fail':\n"
    "    sys.stderr.write('boom\\n'); sys.exit(1)\n"
    "print(f'turns={len(msgs)} last={last}')\n"
)


@pytest.fixture
def stub_interpreter(monkeypatch):
    monkeypatch.setattr(get_settings(), "studio_exec_python", sys.executable)


async def _collect(gen):
    return [chunk async for chunk in gen]


# --- history replay / failed-turn exclusion (pure) ------------------------

def test_construct_messages_list_shape():
    svc = ConversationService()
    sid = "s1"
    svc.sessions[sid] = ConversationSession(session_id=sid)
    svc.messages[sid] = [
        ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, session_id=sid, sender="user", content="q1"),
        ChatMessage(
            workspace_id=DEFAULT_WORKSPACE_ID,
            session_id=sid, sender="agent", content="a1"),
    ]
    assert svc._construct_messages_list(sid) == [
        {"role": "user", "content": [{"text": "q1"}]},
        {"role": "assistant", "content": [{"text": "a1"}]},
    ]


def test_construct_messages_list_excludes_failed_turn_as_pair():
    svc = ConversationService()
    sid = "s2"
    svc.sessions[sid] = ConversationSession(session_id=sid)
    svc.messages[sid] = [
        ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, session_id=sid, sender="user", content="q1"),
        ChatMessage(
            workspace_id=DEFAULT_WORKSPACE_ID,
            session_id=sid, sender="agent", content="a1"),
        ChatMessage(
            workspace_id=DEFAULT_WORKSPACE_ID,
            session_id=sid, sender="user", content="bad", metadata={"error": True}),
        ChatMessage(
            workspace_id=DEFAULT_WORKSPACE_ID,
            session_id=sid, sender="agent", content="err", metadata={"error": True}),
        ChatMessage(workspace_id=DEFAULT_WORKSPACE_ID, session_id=sid, sender="user", content="q3"),
    ]
    roles = [m["role"] for m in svc._construct_messages_list(sid)]
    texts = [m["content"][0]["text"] for m in svc._construct_messages_list(sid)]
    assert roles == ["user", "assistant", "user"]  # alternation preserved
    assert texts == ["q1", "a1", "q3"]  # failed pair dropped


# --- PUT code rewrite -----------------------------------------------------

def test_update_session_code_rewrites_agent_file(stub_interpreter):
    svc = ConversationService()
    session = asyncio.run(
        svc.create_session(CreateConversationRequest(generated_code="print('v1')"))
    )
    sid = session.session_id
    agent_file = svc.agent_processes[sid]["agent_file"]
    assert agent_file.read_text() == "print('v1')"

    asyncio.run(svc.update_session_code(sid, "print('v2')"))
    assert agent_file.read_text() == "print('v2')"
    assert sid in svc.messages  # messages preserved across a code swap


def test_update_session_code_unknown_session_raises():
    svc = ConversationService()
    with pytest.raises(ValueError, match="not found"):
        asyncio.run(svc.update_session_code("nope", "print(1)"))


# --- multi-turn replay (real subprocess, stub interpreter) ----------------

def test_multi_turn_replays_full_history(stub_interpreter):
    svc = ConversationService()
    session = asyncio.run(svc.create_session(CreateConversationRequest(generated_code=ECHO_AGENT)))
    sid = session.session_id

    r1 = asyncio.run(svc.send_message(sid, "hello"))
    assert r1.success is True
    assert r1.content.strip() == "turns=1 last=hello"

    # Second turn sees [user hello, agent reply, user again] = 3 messages.
    r2 = asyncio.run(svc.send_message(sid, "again"))
    assert r2.success is True
    assert r2.content.strip() == "turns=3 last=again"


def test_failed_turn_excluded_from_next_replay(stub_interpreter):
    svc = ConversationService()
    session = asyncio.run(svc.create_session(CreateConversationRequest(generated_code=ECHO_AGENT)))
    sid = session.session_id

    fail = asyncio.run(svc.send_message(sid, "fail"))
    assert fail.success is False
    assert "boom" in fail.error

    # The failed pair is excluded; the next turn only sees [user ok] = 1 message.
    ok = asyncio.run(svc.send_message(sid, "ok"))
    assert ok.success is True
    assert ok.content.strip() == "turns=1 last=ok"


# --- streaming sentinels --------------------------------------------------

def test_stream_success_ends_with_chat_complete(stub_interpreter):
    svc = ConversationService()
    session = asyncio.run(svc.create_session(CreateConversationRequest(generated_code=ECHO_AGENT)))
    sid = session.session_id

    items = asyncio.run(_collect(svc.stream_message(sid, "hi")))
    assert items[-1].startswith("[CHAT_COMPLETE:")
    assert not any(i.startswith("[CHAT_ERROR:") for i in items)
    assert any("turns=1 last=hi" in i for i in items)


def test_stream_failure_emits_chat_error_then_complete(stub_interpreter):
    svc = ConversationService()
    session = asyncio.run(svc.create_session(CreateConversationRequest(generated_code=ECHO_AGENT)))
    sid = session.session_id

    items = asyncio.run(_collect(svc.stream_message(sid, "fail")))
    err = [i for i in items if i.startswith("[CHAT_ERROR:")]
    assert len(err) == 1
    payload = json.loads(err[0][len("[CHAT_ERROR:"): -1])  # JSON-encoded single line
    assert "boom" in payload
    assert items[-1].startswith("[CHAT_COMPLETE:")


# --- endpoint wiring ------------------------------------------------------

def test_conversation_endpoints_create_list_delete(stub_interpreter):
    client = TestClient(create_app())
    resp = client.post("/api/conversations", json={"generated_code": "print('x')"})
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    try:
        listing = client.get("/api/conversations")
        assert any(s["session_id"] == sid for s in listing.json()["sessions"])
        history = client.get(f"/api/conversations/{sid}")
        assert history.status_code == 200
        assert history.json()["messages"] == []
    finally:
        assert client.delete(f"/api/conversations/{sid}").status_code == 200


def test_create_conversation_503_when_interpreter_missing(monkeypatch):
    monkeypatch.setattr(get_settings(), "studio_exec_python", "/no/such/python-xyz")
    client = TestClient(create_app())
    resp = client.post("/api/conversations", json={"generated_code": "print('x')"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "studio.exec.interpreter_unavailable"
    assert "setup_exec_env.sh" in body["message"]
