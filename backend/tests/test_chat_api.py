"""SSE chat generator, api-key auth (401/200/disabled), session persistence."""

from fastapi.testclient import TestClient

import app.routers.agents as agents_router  # noqa: F401 (ensures methods registered)
import app.routers.chat as chat_router
import app.services.chat as chat_service
import app.services.policy_identity as policy_identity
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent, ChatSession
from app.services.chat import chat_stream, sse_encode
from tests.conftest import ws_ctx


def delta_events(text: str):
    return iter(
        {
            "event": "delta",
            "data": {"text": text[index : index + 60]},
        }
        for index in range(0, len(text), 60)
    )


def make_active_agent(method="zip_runtime", name="chat-agent") -> str:
    db = SessionLocal()
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name=name, method=method, status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/x",
        spec={"name": name},
    )
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    return agent_id


def test_chat_stream_buffered_chunks(monkeypatch):
    # Studio runtimes capture stdout as one result, so they stay on the buffered path.
    db = SessionLocal()
    agent = db.get(Agent, make_active_agent(method="studio"))
    db.close()
    monkeypatch.setattr(
        chat_service,
        "invoke_agent_events",
        lambda a, p, session_id=None, actor_id="river", **_kw: delta_events("x" * 150),
    )
    events = list(chat_stream(agent, "hello", workspace=ws_ctx()))
    kinds = [e["event"] for e in events]
    assert kinds[0] == "meta" and kinds[-1] == "done"
    assert events[0]["data"]["mode"] == "buffered"
    deltas = [e for e in events if e["event"] == "delta"]
    assert len(deltas) == 3  # 150 chars / 60-char chunks
    assert "".join(d["data"]["text"] for d in deltas) == "x" * 150


def test_chat_stream_container_forwards_native_events(monkeypatch):
    db = SessionLocal()
    agent = db.get(Agent, make_active_agent(method="container", name="stream-agent"))
    db.close()
    native = [
        {"event": "delta", "data": {"text": "hello "}},
        {"event": "heartbeat", "data": {}},
        {"event": "tool", "data": {"name": "search", "id": "tool-1"}},
        {"event": "delta", "data": {"text": "world"}},
    ]
    monkeypatch.setattr(
        chat_service,
        "invoke_agent_events",
        lambda *args, **kwargs: iter(native),
    )

    events = list(chat_stream(agent, "hello", workspace=ws_ctx()))

    assert events[0]["data"]["mode"] == "stream"
    assert events[1:-1] == native
    assert events[-1]["event"] == "done"


def test_zip_runtime_streams_native_runtime_events_like_harness(monkeypatch):
    """STRANDS · ZIP agents must show live deltas and tool calls in the playground,
    not a 60-char buffered replay — same contract as harness/container."""
    import app.services.invoke as invoke_service

    db = SessionLocal()
    agent = db.get(Agent, make_active_agent(method="zip_runtime", name="zip-stream"))
    db.close()

    class Body:
        def iter_lines(self, *, chunk_size):
            yield from [
                b'data: {"event":"delta","text":"hello "}',
                b"",
                b'data: {"event":"tool","name":"calculator","id":"tool-1"}',
                b"",
                b'data: {"event":"delta","text":"world"}',
                b"",
                b'data: {"event":"complete","result":"hello world"}',
                b"",
            ]

    class StreamingDataPlane:
        def __init__(self):
            self.invoked_with = None

        def invoke_agent_runtime(self, **kwargs):
            self.invoked_with = kwargs
            return {"response": Body(), "contentType": "text/event-stream"}

    data = StreamingDataPlane()
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(invoke_service.canary_service, "active_canary_route", lambda _id: None)

    events = list(chat_stream(agent, "hello", session_id="s" * 40, workspace=ws_ctx()))

    assert events[0]["data"]["mode"] == "stream"
    assert [e["event"] for e in events] == ["meta", "delta", "tool", "delta", "done"]
    assert events[2]["data"] == {"name": "calculator", "id": "tool-1"}
    assert "".join(e["data"]["text"] for e in events if e["event"] == "delta") == "hello world"
    assert data.invoked_with["agentRuntimeArn"] == agent.arn


def test_zip_runtime_legacy_json_result_still_renders(monkeypatch):
    """A zip runtime deployed from the pre-streaming template answers JSON; the
    native path must turn that into one delta instead of failing."""
    import app.services.invoke as invoke_service

    db = SessionLocal()
    agent = db.get(Agent, make_active_agent(method="zip_runtime", name="zip-legacy"))
    db.close()

    class Body:
        def read(self):
            return b'{"result": "plain answer"}'

    class JsonDataPlane:
        def invoke_agent_runtime(self, **_kwargs):
            return {"response": Body(), "contentType": "application/json"}

    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: JsonDataPlane())
    monkeypatch.setattr(invoke_service.canary_service, "active_canary_route", lambda _id: None)

    events = list(chat_stream(agent, "hello", session_id="s" * 40, workspace=ws_ctx()))

    assert [e["event"] for e in events] == ["meta", "delta", "done"]
    assert events[1]["data"]["text"] == "plain answer"


def test_chat_stream_error_event(monkeypatch):
    db = SessionLocal()
    agent = db.get(Agent, make_active_agent(name="chat-agent-err"))
    db.close()

    def boom(*a, **k):
        raise RuntimeError("runtime unavailable")

    monkeypatch.setattr(chat_service, "invoke_agent_events", boom)
    events = list(chat_stream(agent, "hello", workspace=ws_ctx()))
    assert events[-1]["event"] == "error"
    assert "runtime unavailable" in events[-1]["data"]["message"]


def test_sse_encode_format():
    line = sse_encode({"event": "delta", "data": {"text": "hi"}})
    assert line == 'event: delta\ndata: {"text": "hi"}\n\n'


def test_sse_encode_heartbeat_as_comment():
    line = sse_encode({"event": "heartbeat", "data": {}})
    assert line == ": keep-alive\n\n"


def test_api_key_auth_matrix(client):
    # no key → 401
    res = client.get("/v1/agents")
    assert res.status_code == 401 and res.json()["code"] == "auth.missing_api_key"

    # create a key → 200
    created = client.post("/api/apikeys", json={"name": "test"}).json()
    raw = created["key"]
    assert raw.startswith("lp_live_")
    ok = client.get("/v1/agents", headers={"X-Api-Key": raw})
    assert ok.status_code == 200

    # bogus key → 401
    bad = client.get("/v1/agents", headers={"X-Api-Key": "lp_live_wrong"})
    assert bad.status_code == 401 and bad.json()["code"] == "auth.invalid_api_key"

    # disabled key → 401
    client.post(f"/api/apikeys/{created['id']}/disable")
    disabled = client.get("/v1/agents", headers={"X-Api-Key": raw})
    assert disabled.status_code == 401


def test_api_keys_hashed_at_rest(client):
    created = client.post("/api/apikeys", json={"name": "hashcheck"}).json()
    raw = created["key"]
    db = SessionLocal()
    from app.models.ledger import ApiKey

    row = db.get(ApiKey, created["id"])
    assert raw not in (row.key_hash or "")
    assert len(row.key_hash) == 64  # sha256 hex
    assert row.prefix.endswith("…") and len(row.prefix) <= 16
    db.close()
    listed = client.get("/api/apikeys").json()["keys"]
    assert all("key" not in k for k in listed)  # full key never listed


def test_chat_endpoint_tracks_session(client, monkeypatch):
    agent_id = make_active_agent(name="chat-sess-agent")
    monkeypatch.setattr(
        chat_service,
        "invoke_agent_events",
        lambda a, p, session_id=None, actor_id="river", **_kw: delta_events("ok"),
    )
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi"})
    assert res.status_code == 200
    body = res.text
    assert "event: meta" in body and "event: delta" in body and "event: done" in body
    sessions = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"]
    assert len(sessions) == 1 and sessions[0]["turns"] == 1


def test_chat_heartbeat_is_not_persisted(client, monkeypatch):
    agent_id = make_active_agent(method="container", name="chat-heartbeat-agent")
    monkeypatch.setattr(
        chat_service,
        "invoke_agent_events",
        lambda *args, **kwargs: iter(
            [
                {"event": "heartbeat", "data": {}},
                {"event": "delta", "data": {"text": "done"}},
            ]
        ),
    )

    response = client.post(f"/api/chat/{agent_id}", json={"prompt": "slow task"})

    assert response.status_code == 200
    assert ": keep-alive\n\n" in response.text
    assert "event: heartbeat" not in response.text
    session_id = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"][0][
        "session_id"
    ]
    history = client.get(
        f"/api/chat/{agent_id}/history", params={"session_id": session_id}
    ).json()["messages"]
    assert [(message["role"], message["text"]) for message in history] == [
        ("user", "slow task"),
        ("agent", "done"),
    ]


def test_chat_history_persists_and_replays(client, monkeypatch):
    """Thread items are stored in event order and replayed by /history; the
    sessions list carries a first-prompt preview."""
    agent_id = make_active_agent(name="chat-hist-agent")
    monkeypatch.setattr(
        chat_service,
        "invoke_agent_events",
        lambda a, p, session_id=None, actor_id="river", **_kw: delta_events(f"echo: {p}"),
    )
    client.post(f"/api/chat/{agent_id}", json={"prompt": "first question"})
    sid = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"][0]["session_id"]
    client.post(f"/api/chat/{agent_id}",
                json={"prompt": "second question", "session_id": sid})

    history = client.get(
        f"/api/chat/{agent_id}/history", params={"session_id": sid}
    ).json()["messages"]
    assert [(m["role"], m["text"]) for m in history] == [
        ("user", "first question"), ("agent", "echo: first question"),
        ("user", "second question"), ("agent", "echo: second question"),
    ]

    sessions = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"]
    assert sessions[0]["preview"] == "first question"
    assert sessions[0]["turns"] == 2


def test_sessions_without_transcript_hidden(client, monkeypatch):
    """Sessions that predate the ChatMessage ledger have nothing to replay —
    the sessions list must not offer them (they opened as empty threads)."""
    from app.models.ledger import ChatSession

    agent_id = make_active_agent(name="chat-legacy-agent")
    db = SessionLocal()
    db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id,
                       session_id="legacy" + "x" * 40, turns=3))
    db.commit()
    db.close()
    assert client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"] == []

    monkeypatch.setattr(
        chat_service,
        "invoke_agent_events",
        lambda a, p, session_id=None, actor_id="river", **_kw: delta_events("ok"),
    )
    client.post(f"/api/chat/{agent_id}", json={"prompt": "hi"})
    sessions = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"]
    assert len(sessions) == 1  # the legacy row stays hidden
    assert not sessions[0]["session_id"].startswith("legacy")
    assert sessions[0]["preview"] == "hi"


def test_chat_history_records_errors(client, monkeypatch):
    """A failed turn keeps the user prompt and stores the error row."""
    agent_id = make_active_agent(name="chat-hist-err")

    def boom(*a, **k):
        raise RuntimeError("runtime exploded")

    monkeypatch.setattr(chat_service, "invoke_agent_events", boom)
    client.post(f"/api/chat/{agent_id}", json={"prompt": "doomed", "session_id": "e" * 40})
    history = client.get(
        f"/api/chat/{agent_id}/history", params={"session_id": "e" * 40}
    ).json()["messages"]
    assert [m["role"] for m in history] == ["user", "error"]
    assert "runtime exploded" in history[1]["text"]


def test_v1_and_console_share_invoke_chain():
    """Code-level proof: both surfaces call the same chain functions."""
    import inspect

    import app.routers.chat as chat_router
    import app.routers.public_api as public_api

    chat_src = inspect.getsource(chat_router)
    v1_src = inspect.getsource(public_api)
    assert "chat_stream" in chat_src and "chat_stream" in v1_src
    assert "invoke_agent_text" in v1_src  # sync path shared with agents router


def test_authenticated_chat_identity_cannot_be_spoofed(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
    get_settings.cache_clear()
    captured: dict = {}
    try:
        agent_id = make_active_agent(name="trusted-chat-user")
        db = SessionLocal()
        agent = db.get(Agent, agent_id)
        agent.spec = {
            "name": agent.name,
            "tools": [{"type": "gateway", "name": "hr-database", "config": {}}],
        }
        db.commit()
        db.close()
        monkeypatch.setattr(
            policy_identity,
            "gateway_user_token",
            lambda _ws, username, role, email=None: (
                captured.update(policy_user=username, policy_role=role) or "trusted-jwt"
            ),
        )

        def fake_stream(agent, prompt, **kwargs):
            captured.update(kwargs)
            yield {
                "event": "meta",
                "data": {"session_id": "s" * 40, "agent": agent.name, "mode": "buffered"},
            }
            yield {"event": "done", "data": {"latency_ms": 1}}

        monkeypatch.setattr(chat_router, "chat_stream", fake_stream)
        with TestClient(create_app()) as client:
            login = client.post(
                "/api/auth/login",
                json={"username": "operator", "password": "s3cret-pass"},
            )
            assert login.status_code == 200
            response = client.post(
                f"/api/chat/{agent_id}",
                json={"prompt": "hello", "actor_id": "river"},
            )
            assert response.status_code == 200

        assert captured["policy_user"] == "operator"
        assert captured["policy_role"] == "admin"
        assert captured["actor_id"] == f"{agent_id}__operator"
        assert captured["runtime_user_id"] == "operator"
        assert captured["gateway_access_token"] == "trusted-jwt"
        db = SessionLocal()
        row = db.query(ChatSession).filter_by(agent_id=agent_id).one()
        db.close()
        assert row.actor_id == "operator"
    finally:
        get_settings.cache_clear()
