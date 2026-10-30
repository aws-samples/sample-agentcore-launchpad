"""SSE chat generator, api-key auth (401/200/disabled), session persistence."""

import pytest
from fastapi.testclient import TestClient

import app.routers.agents as agents_router  # noqa: F401 (ensures methods registered)
import app.routers.chat as chat_router
import app.services.chat as chat_service
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.models.ledger import Agent, ChatSession
from app.services.agentcore import runtime as rt
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

    events = list(chat_stream(agent, "hello", session_id="s" * 40, workspace=ws_ctx()))

    assert events[0]["data"]["mode"] == "stream"
    assert [e["event"] for e in events] == ["meta", "delta", "tool", "delta", "done"]
    assert events[2]["data"] == {"name": "calculator", "id": "tool-1"}
    assert "".join(e["data"]["text"] for e in events if e["event"] == "delta") == "hello world"
    assert data.invoked_with["agentRuntimeArn"] == agent.arn


def test_harness_streams_native_runtime_events_like_zip_runtime(monkeypatch):
    """A self-built harness row is Runtime-backed exactly like zip_runtime — same
    SSE delta/tool/complete parsing, same invoke chain, no special-case refusal."""
    import app.services.invoke as invoke_service

    db = SessionLocal()
    agent = db.get(Agent, make_active_agent(method="harness", name="harness-stream"))
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


def test_a2a_chat_surfaces_the_peer_failure_as_an_error_event(client, monkeypatch):
    """N10v3-bug5: a direct Chat with an A2A agent streamed `meta … done` with no text
    and no error. The buffered A2A path now raises on a failed/empty Task, and the
    stream turns that into an `error` event — never silence."""
    import app.services.invoke as inv

    db = SessionLocal()
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name="chat-a2a", method="zip_runtime",
        status="active", arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/a",
        spec={"name": "chat-a2a", "protocol": "a2a"},
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    db.close()

    monkeypatch.setattr(inv, "data_client", lambda _ws=None: object())

    def failed_task(*a, **k):
        raise AppError("agent.invoke_failed",
                       "A2A task failed: RuntimeError: no workload access token", status_code=502)

    monkeypatch.setattr(inv.rt, "invoke_a2a_text", failed_task)
    events = list(chat_stream(agent, "hello", workspace=ws_ctx()))
    # the stream ends on the error (as every chat error does) — and carries no empty delta
    assert [e["event"] for e in events] == ["meta", "error"]
    assert "no workload access token" in events[1]["data"]["message"]
    assert events[1]["data"]["code"] == "agent.invoke_failed"
    assert events[0]["data"]["mode"] == "buffered"

    # review F5: the public API answers 502 with the reason, not a bare 500
    raw = client.post("/api/apikeys", json={"name": "a2a-502"}).json()["key"]
    res = client.post(f"/v1/agents/{agent.id}/invoke", json={"prompt": "hello"},
                      headers={"X-Api-Key": raw})
    assert res.status_code == 502, res.text
    assert res.json()["code"] == "agent.invoke_failed"
    assert "no workload access token" in res.json()["message"]


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


def test_chat_stream_meta_carries_no_inbound_key(monkeypatch):
    """R1 (N3 W2 Task 3): the "invoke as me" toggle and its meta["inbound"]
    caller-naming are gone — AWS_IAM is the only mode, so there is nothing to
    name. ``chat_stream`` also no longer accepts a ``bearer_token`` kwarg."""
    import inspect

    assert "bearer_token" not in inspect.signature(chat_stream).parameters
    agent_id = make_active_agent(name="chat-meta-agent")
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    monkeypatch.setattr(chat_service, "invoke_agent_events", lambda *_a, **_kw: iter(()))
    meta = next(chat_stream(agent, "hi"))["data"]
    db.close()
    assert "inbound" not in meta


def test_chat_endpoint_refuses_the_retired_as_user_field(client, monkeypatch):
    """``as_user`` was retired with the Cognito inbound-auth path (no console sends it);
    N16-obs78: like every other unknown key it is 422 ``validation.invalid_request``
    naming the key, not silently ignored."""
    agent_id = make_active_agent(name="chat-as-user-agent")
    monkeypatch.setattr(
        chat_service, "invoke_agent_events",
        lambda a, p, session_id=None, actor_id="river", **_kw: delta_events("ok"),
    )
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi", "as_user": True})
    assert res.status_code == 422
    assert res.json()["code"] == "validation.invalid_request"
    assert [e["loc"][-1] for e in res.json()["detail"]] == ["as_user"]


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


def test_chat_history_records_policy_denials(client, monkeypatch):
    """A Gateway policy denial is stored after its tool row and replayed as a
    ``policy`` row (reason in ``text``, catalog tool name in ``name``)."""
    agent_id = make_active_agent(name="chat-hist-policy")

    def events(*_a, **_k):
        yield {"event": "tool", "data": {"name": "gw_create_payout", "id": "t-1"}}
        yield {"event": "policy_denied", "data": {
            "tool": "hr-database___create_payout", "tool_use_id": "t-1",
            "reason": "Policy evaluation denied due to cap-1", "policy_id": "cap-1",
            # A6 deny-card facts (additive): kept on the row, replayed under `policy`;
            # N17-obs526: + the deciding gateway and its engine (the restored card's link)
            "gateway_id": "gw-1", "engine_id": "pe-1",
            "policy_name": "payout cap", "stage": "bt", "source": "gateway",
            "decision_id": None,
        }}
        yield from delta_events("blocked by policy")

    monkeypatch.setattr(chat_service, "invoke_agent_events", events)
    response = client.post(
        f"/api/chat/{agent_id}", json={"prompt": "pay 800", "session_id": "p" * 40}
    )
    assert "event: policy_denied" in response.text
    history = client.get(
        f"/api/chat/{agent_id}/history", params={"session_id": "p" * 40}
    ).json()["messages"]
    assert [(m["role"], m["text"], m["name"]) for m in history] == [
        ("user", "pay 800", None),
        ("tool", "", "gw_create_payout"),
        ("policy", "Policy evaluation denied due to cap-1", "hr-database___create_payout"),
        ("agent", "blocked by policy", None),
    ]
    assert history[2]["policy"] == {"policy_id": "cap-1", "policy_name": "payout cap",
                                    "stage": "bt", "source": "gateway",
                                    "gateway_id": "gw-1", "engine_id": "pe-1"}
    assert "policy" not in history[1]


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
        # A bare gateway ToolRef no longer needs runtimeUserId (SigV4, not an
        # outbound token exchange) — a tool-level auth block still does, so
        # that is what exercises this anti-spoofing path now.
        agent.spec = {
            "name": agent.name,
            "tools": [{
                "type": "rest", "name": "hr-api", "config": {"url": "https://hr.example"},
                "auth": {"connection": "hr-conn", "kind": "oauth2", "mode": "as_agent"},
            }],
        }
        db.commit()
        db.close()

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
            # N16-obs78: a body naming the actor is refused outright (422 naming the key)
            # rather than silently ignored — nothing streams, no session row is written
            spoof = client.post(
                f"/api/chat/{agent_id}",
                json={"prompt": "hello", "actor_id": "river"},
            )
            assert spoof.status_code == 422, spoof.text
            assert spoof.json()["code"] == "validation.invalid_request"
            assert [e["loc"][-1] for e in spoof.json()["detail"]] == ["actor_id"]
            assert captured == {}
            response = client.post(f"/api/chat/{agent_id}", json={"prompt": "hello"})
            assert response.status_code == 200

        assert captured["actor_id"] == f"{agent_id}__operator"
        assert captured["runtime_user_id"] == "operator"
        db = SessionLocal()
        row = db.query(ChatSession).filter_by(agent_id=agent_id).one()
        db.close()
        assert row.actor_id == "operator"
    finally:
        get_settings.cache_clear()


def test_chat_routes_answer_404_for_a_deleted_agent(client):
    """N10v2-bug3: ``POST /api/chat/{id}`` on a deleted agent said 409
    ``agent.not_active``; ``/v1`` and the agents API say 404 — the console agrees now,
    on every chat sub-route."""
    agent_id = make_active_agent(name="gone-agent")
    db = SessionLocal()
    db.get(Agent, agent_id).status = "deleted"
    db.commit()
    db.close()
    for method, path in (
        ("POST", f"/api/chat/{agent_id}"),
        ("GET", f"/api/chat/{agent_id}/sessions"),
        ("GET", f"/api/chat/{agent_id}/history?session_id=s1"),
        ("GET", f"/api/chat/{agent_id}/memory?session_id=s1"),
        ("POST", f"/api/chat/{agent_id}/sessions/s1/stop"),
    ):
        res = client.request(method, path, json={"prompt": "hi"} if method == "POST" else None)
        assert res.status_code == 404, (method, path, res.text)
        assert res.json()["code"] == "agent.not_found"


# --- N-SCN-obs1: /v1 sessions are listed, marked `source: public_api` ---------------


def _mint_key(client) -> str:
    return client.post("/api/apikeys", json={"name": "v1-sessions"}).json()["key"]


# ── N-SCN-obs16: the runtimeSessionId bounds are checked up front ──────────

_SHORT_SID = "s" * 32
_LONG_SID = "s" * 101


def _assert_session_id_invalid(res, length: int) -> None:
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "chat.session_id_invalid"
    assert "33" in body["message"] and "100" in body["message"]
    assert body["detail"] == {"length": length, "min_length": 33, "max_length": 100}


@pytest.mark.parametrize("sid", [_SHORT_SID, _LONG_SID], ids=["32-chars", "101-chars"])
def test_chat_route_refuses_an_out_of_range_session_id_before_the_stream(
    client, monkeypatch, sid,
):
    """A 13-char test id used to end the turn in an `error` row — `ParamValidationError:
    Invalid length for parameter runtimeSessionId` — after the policy gate had recorded
    its decision. Now it is a 422 naming the 33–100 rule, before anything runs."""
    agent_id = make_active_agent(name="sid-chat")
    monkeypatch.setattr(
        chat_router, "chat_stream", lambda *a, **k: pytest.fail("chat_stream was reached"),
    )
    res = client.post(f"/api/chat/{agent_id}", json={"prompt": "hi", "session_id": sid})
    _assert_session_id_invalid(res, len(sid))
    db = SessionLocal()
    try:  # no session row for the refused turn
        assert db.query(ChatSession).filter(ChatSession.session_id == sid).count() == 0
    finally:
        db.close()


@pytest.mark.parametrize("sid", [_SHORT_SID, _LONG_SID], ids=["32-chars", "101-chars"])
def test_v1_routes_refuse_an_out_of_range_session_id_before_the_gate(client, monkeypatch, sid):
    import app.routers.public_api as public_api

    agent_id = make_active_agent(name="sid-v1")
    monkeypatch.setattr(
        public_api, "invoke_agent_text", lambda *a, **k: pytest.fail("invoke was reached"),
    )
    monkeypatch.setattr(
        public_api, "chat_stream", lambda *a, **k: pytest.fail("the stream was opened"),
    )
    headers = {"X-Api-Key": _mint_key(client)}
    for path in ("invoke", "invoke-stream"):
        res = client.post(f"/v1/agents/{agent_id}/{path}",
                          json={"prompt": "hi", "session_id": sid}, headers=headers)
        _assert_session_id_invalid(res, len(sid))


def test_console_invoke_route_refuses_an_out_of_range_session_id(client, monkeypatch):
    agent_id = make_active_agent(name="sid-console")
    monkeypatch.setattr(
        agents_router, "invoke_agent_text", lambda *a, **k: pytest.fail("invoke was reached"),
    )
    res = client.post(f"/api/agents/{agent_id}/invoke",
                      json={"prompt": "hi", "session_id": _SHORT_SID})
    _assert_session_id_invalid(res, 32)


def test_session_ids_at_the_bounds_and_generated_ids_pass(client, monkeypatch):
    import app.routers.public_api as public_api
    from app.services.agentcore import runtime as rt

    agent_id = make_active_agent(name="sid-ok")
    seen: list[str | None] = []

    def fake_invoke(agent, prompt, session_id=None, **_kw):
        seen.append(session_id)
        return {"text": "ok", "session_id": session_id or rt.new_session_id()}

    monkeypatch.setattr(public_api, "invoke_agent_text", fake_invoke)
    headers = {"X-Api-Key": _mint_key(client)}
    for sid in ("s" * 33, "s" * 100, None):
        res = client.post(f"/v1/agents/{agent_id}/invoke",
                          json={"prompt": "hi", "session_id": sid}, headers=headers)
        assert res.status_code == 200, res.text
        assert rt.SESSION_ID_MIN_LEN <= len(res.json()["session_id"]) <= rt.SESSION_ID_MAX_LEN
    assert seen == ["s" * 33, "s" * 100, None]
    assert len(rt.new_session_id()) == 64  # generated ids are untouched by the rule


def test_v1_sync_invoke_session_is_listed_with_public_api_source(client, monkeypatch):
    """`POST /v1/agents/{id}/invoke` used to persist nothing, so the console's
    session list (transcript-bearing rows only) never showed it. Now the turn is
    ledgered like a console turn and carries `source: public_api`."""
    import app.routers.public_api as public_api

    agent_id = make_active_agent(name="v1-sess-agent")
    monkeypatch.setattr(
        public_api, "invoke_agent_text",
        lambda agent, prompt, session_id=None, **_kw: {
            "text": "hello from v1", "session_id": session_id or "v1" + "s" * 31,
        },
    )
    res = client.post(f"/v1/agents/{agent_id}/invoke", json={"prompt": "hi via v1"},
                      headers={"X-Api-Key": _mint_key(client)})
    assert res.status_code == 200, res.text
    sid = res.json()["session_id"]

    sessions = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"]
    assert [s["session_id"] for s in sessions] == [sid]
    assert sessions[0]["source"] == "public_api"
    assert sessions[0]["actor_id"] == "api"  # the bare caller actor, not the memory-scoped one
    assert sessions[0]["turns"] == 1 and sessions[0]["preview"] == "hi via v1"

    history = client.get(f"/api/chat/{agent_id}/history?session_id={sid}").json()["messages"]
    assert [(m["role"], m["text"]) for m in history] == [
        ("user", "hi via v1"), ("agent", "hello from v1"),
    ]

    # a second turn on the same id counts, and the row keeps its entrance
    res = client.post(f"/v1/agents/{agent_id}/invoke",
                      json={"prompt": "again", "session_id": sid},
                      headers={"X-Api-Key": _mint_key(client)})
    assert res.status_code == 200
    sessions = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"]
    assert len(sessions) == 1 and sessions[0]["turns"] == 2
    assert sessions[0]["source"] == "public_api"


def test_v1_stream_session_is_listed_with_public_api_source(client, monkeypatch):
    agent_id = make_active_agent(name="v1-stream-agent")
    monkeypatch.setattr(
        chat_service, "invoke_agent_events",
        lambda a, p, session_id=None, actor_id="river", **_kw: iter([
            {"event": "delta", "data": {"text": "part one "}},
            {"event": "tool", "data": {"name": "kb_search"}},
            {"event": "delta", "data": {"text": "part two"}},
        ]),
    )
    res = client.post(f"/v1/agents/{agent_id}/invoke-stream", json={"prompt": "stream me"},
                      headers={"X-Api-Key": _mint_key(client)})
    assert res.status_code == 200, res.text
    assert "event: meta" in res.text and "event: done" in res.text

    sessions = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["source"] == "public_api" and sessions[0]["preview"] == "stream me"
    sid = sessions[0]["session_id"]
    history = client.get(f"/api/chat/{agent_id}/history?session_id={sid}").json()["messages"]
    assert [(m["role"], m["text"], m.get("name")) for m in history] == [
        ("user", "stream me", None),
        ("agent", "part one ", None),
        ("tool", "", "kb_search"),
        ("agent", "part two", None),
    ]


def test_console_session_source_is_console(client, monkeypatch):
    agent_id = make_active_agent(name="console-src-agent")
    monkeypatch.setattr(
        chat_service, "invoke_agent_events",
        lambda a, p, session_id=None, actor_id="river", **_kw: delta_events("ok"),
    )
    client.post(f"/api/chat/{agent_id}", json={"prompt": "hi"})
    sessions = client.get(f"/api/chat/{agent_id}/sessions").json()["sessions"]
    assert len(sessions) == 1 and sessions[0]["source"] == "console"


def test_chat_path_raises_on_the_sdk_error_frame_with_error_type_and_bounded_text():
    """N-ARCH-obs18 (rev-r11 nit): the chat path and the architect share one detector
    — a frame with an empty ``error`` but ``error_type`` set still raises, the text is
    bounded, and ``error_type`` is structured on the error."""
    with pytest.raises(rt.SdkStreamError) as exc:
        list(rt._runtime_payload_events({"error": "", "error_type": "ModelThrottledException",
                                         "message": "An error occurred during streaming"}))
    assert exc.value.error_type == "ModelThrottledException"
    assert exc.value.detail["error_type"] == "ModelThrottledException"
    assert str(exc.value) == ("runtime returned error: ModelThrottledException "
                              "(An error occurred during streaming)")

    with pytest.raises(rt.SdkStreamError) as exc:
        list(rt._runtime_payload_events({"error": "x" * 5000, "error_type": "ValidationException",
                                         "message": "An error occurred during streaming"}))
    assert len(str(exc.value)) < rt.STREAM_ERROR_TEXT_MAX + 200
    assert "[+4400 chars]" in str(exc.value) and exc.value.detail["error_type"] == (
        "ValidationException")
    # still a RuntimeError, as every chat consumer expects; the plain shape is unchanged
    assert isinstance(exc.value, RuntimeError)
    with pytest.raises(RuntimeError, match="runtime returned error: boom"):
        list(rt._runtime_payload_events({"error": "boom"}))
    # the Harness's own event kinds and Converse events are not error frames
    assert list(rt._runtime_payload_events({"event": "delta", "text": "hi"})) == [
        {"event": "delta", "data": {"text": "hi"}}]
