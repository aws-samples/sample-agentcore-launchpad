"""The COA playground (`POST /api/context-layer/playground`): COA Serve frames
normalized to Launchpad SSE events, the consent short-circuit, COA's refusals,
and that the upstream stream closes when the console stops reading.

Hermetic: COA's Serve runtime is ``coa_fakes.CoaHttp`` (recorded frames) behind
``httpx.MockTransport``; the disconnect/heartbeat tests drive the generator directly.
"""

import asyncio
import json
import time

import httpx
import pytest

from app.models.ledger import OauthPendingSession
from app.services import context_layer
from app.services import context_layer_playground as pg
from app.services import context_layer_tokens as tokens

from . import coa_fakes as fx

URL = "/api/context-layer/playground"
QUESTION = "每个部门各有多少名员工？"


@pytest.fixture
def account(monkeypatch):
    account = fx.install(monkeypatch)
    fx.connect_default(account)
    account.data.token = fx.jwt(time.time() + 3600, sub="u-1")
    return account


@pytest.fixture
def coa(monkeypatch):
    return fx.install_http(monkeypatch)


def events(text: str) -> list[tuple[str, dict]]:
    """Parse the SSE text the route answered (keep-alive comments dropped)."""
    out = []
    for block in text.split("\n\n"):
        lines = [line for line in block.splitlines() if not line.startswith(":")]
        if not lines:
            continue
        kind = next(line[6:].strip() for line in lines if line.startswith("event:"))
        data = json.loads("".join(line[5:].strip() for line in lines if line.startswith("data:")))
        out.append((kind, data))
    return out


def _ask(client, **body):
    return client.post(URL, json={"namespace_id": fx.NAMESPACE, "query": QUESTION, **body})


# ── normalization ────────────────────────────────────────────────────────────


def test_a_step_frame_keeps_name_status_timing_and_a_bounded_detail():
    (event,) = pg.normalize(fx.SERVE_FRAMES[1])
    assert event == {"event": "step", "data": {
        "name": "T2_NL_TO_SQL", "status": "success", "duration_ms": 9800, "tool": "athena",
        "detail": '{"table": "employees"}',
    }}
    long = {"type": "step", "payload": {"stepName": "x", "status": "ok", "detail": "d" * 900}}
    assert len(pg.normalize(long)[0]["data"]["detail"]) <= pg.STEP_DETAIL_MAX + 1


def test_a_token_frame_is_answer_text():
    assert pg.normalize(fx.SERVE_FRAMES[2]) == [
        {"event": "token", "data": {"text": "各部门员工数如下。"}}
    ]
    assert pg.normalize({"type": "token", "payload": {"text": ""}}) == []


def test_done_carries_the_answer_tier_confidence_rows_and_coas_session():
    (event,) = pg.normalize(fx.SERVE_FRAMES[3])
    data = event["data"]
    assert event["event"] == "done"
    assert data["answer"].startswith("共 6 个部门")
    assert (data["tier"], data["confidence"], data["rationale"]) == (2.0, 0.92, "governed table")
    assert data["columns"] == ["department", "employee_count"]
    assert data["rows"][0] == {"department": "Engineering", "employee_count": 41}
    assert (data["row_count"], data["rows_truncated"]) == (6, False)
    # COA's generated SQL exposes internal schema: never forwarded, even when COA sends it
    assert "query_used" not in data and "SELECT" not in json.dumps(data)
    assert data["data_sources"] == ["anyhr_glue"] and data["total_ms"] == 17200
    assert data["session_id"] == fx.COA_SESSION
    assert "never-projected" not in json.dumps(data)


def test_done_caps_rows_and_bounds_cells():
    rows = [{"n": i, "blob": {"deep": "x" * 5000}} for i in range(pg.MAX_ROWS + 50)]
    data = pg.done_data({"tier": 1, "confidence": {"score": 1}, "resultRows": rows}, None)
    assert len(data["rows"]) == pg.MAX_ROWS and data["row_count"] == pg.MAX_ROWS + 50
    assert data["rows_truncated"] is True
    assert len(data["rows"][0]["blob"]) <= pg.CELL_MAX + 1


@pytest.mark.parametrize("payload,code", [
    ({"error": "AccessDenied", "message": "Access denied", "statusCode": 403},
     "context_layer.forbidden"),
    ({"error": "NoResult", "message": "No answer found", "statusCode": 404},
     "context_layer.no_result"),
    ({"error": "NamespaceNotFoundError", "message": "gone", "statusCode": 404},
     "context_layer.coa_not_found"),
    ({"error": "AmbiguousReferenceError", "message": "which one?", "statusCode": 400},
     "context_layer.coa_rejected"),
    ({"error": "TimeoutError", "message": "Query resolution timed out", "statusCode": 504},
     "context_layer.upstream"),
    ({"error": "Unauthorized", "statusCode": 401}, "context_layer.reauthorize"),
])
def test_coa_errors_map_to_named_codes(payload, code):
    (event,) = pg.normalize({"type": "error", "payload": payload})
    assert event["event"] == "error" and event["data"]["code"] == code
    assert event["data"]["status"] == payload["statusCode"]
    assert event["data"]["message"]


def test_a_bare_pre_stream_refusal_and_a_non_streamed_result_are_understood():
    refusal = {"error": "AccessDeniedError", "message": "not permitted to query this namespace",
               "requestId": "r", "statusCode": 403}
    assert pg.normalize(refusal)[0]["data"]["code"] == "context_layer.forbidden"
    result = {"result": {"tier": 3, "confidence": {"score": 0.4}, "trace": []},
              "sessionId": "s-1"}
    assert pg.normalize(result)[0]["data"]["session_id"] == "s-1"
    # an id the console could not send back (the route's pattern) is not echoed
    for odd in ("a b", "x" * 129, 42):
        assert pg.normalize({**result, "sessionId": odd})[0]["data"]["session_id"] is None
    assert pg.normalize("keepalive") == [] and pg.normalize({"type": "unknown"}) == []


def test_the_runtime_session_is_long_enough_and_stable_per_member_and_namespace():
    one = pg.runtime_session_id("default", "alice", fx.NAMESPACE)
    assert len(one) >= 33 and one == pg.runtime_session_id("default", "alice", fx.NAMESPACE)
    assert one != pg.runtime_session_id("default", "bob", fx.NAMESPACE)
    assert one != pg.runtime_session_id("ws-2", "alice", fx.NAMESPACE)


# ── the route ────────────────────────────────────────────────────────────────


def test_the_playground_streams_steps_tokens_and_done_as_the_member(client, account, coa):
    response = _ask(client, mode="standard", strategy="nl_to_sql_first")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    kinds = [kind for kind, _ in events(response.text)]
    assert kinds == ["step", "step", "token", "done"]
    assert events(response.text)[-1][1]["rows"][5] == {"department": "Sales",
                                                       "employee_count": 8}
    (request,) = coa.requests
    layer = _layer()
    assert str(request.url) == layer.serve_url
    assert request.headers["authorization"] == f"Bearer {account.data.token}"
    session = request.headers["x-amzn-bedrock-agentcore-runtime-session-id"]
    assert session == pg.runtime_session_id("default", "admin", fx.NAMESPACE)
    # exactly this body: in particular no ``includeDebugInfo`` (COA's schema-exposing SQL)
    assert json.loads(request.content) == {
        "query": QUESTION, "namespace": fx.NAMESPACE, "stream": True,
        "options": {"mode": "standard", "strategy": "nl_to_sql_first"},
    }
    assert account.data.token not in response.text


def test_a_follow_up_turn_sends_coas_session_back(client, account, coa):
    _ask(client, session_id=fx.COA_SESSION)
    body = json.loads(coa.requests[-1].content)
    assert body["sessionId"] == fx.COA_SESSION and "options" not in body


def test_no_consent_is_one_auth_required_event_and_coa_is_never_called(client, account, coa):
    account.data.token = None
    response = _ask(client)
    assert response.status_code == 200
    ((kind, data),) = events(response.text)
    assert kind == "auth_required"
    assert data == {
        "provider": "coa-coa-w2", "tool": "context_layer",
        "url": "https://coa-auth.example/authorize?state=x",
        "scopes": ["openid", "email", "profile"], "agent_id": "",
    }
    assert coa.requests == []
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        pending = db.query(OauthPendingSession).one()
    finally:
        db.close()
    assert (pending.agent_id, pending.tool) == ("", "context_layer")


def test_a_coa_401_is_a_reauthorize_event_and_forces_the_next_consent(client, account, coa):
    coa.overrides["serve"] = httpx.Response(401, json={"message": "Token expired"})
    ((kind, data),) = events(_ask(client).text)
    assert (kind, data["code"], data["status"]) == ("error", "context_layer.reauthorize", 401)
    assert ("default", "admin") in tokens._FORCE
    # the next turn asks for consent again instead of re-sending the rejected token
    ((kind, _),) = events(_ask(client).text)
    assert kind == "auth_required"


def test_an_in_stream_denial_ends_the_stream_with_forbidden(client, account, coa):
    coa.serve_frames = [{"error": "AccessDeniedError", "message": "not permitted",
                         "requestId": "r", "statusCode": 403}]
    ((kind, data),) = events(_ask(client).text)
    assert (kind, data["code"], data["message"]) == (
        "error", "context_layer.forbidden", "not permitted",
    )


@pytest.mark.parametrize("status,code", [(403, "context_layer.forbidden"),
                                         (503, "context_layer.upstream")])
def test_a_runtime_refusal_names_the_status_only(client, account, coa, status, code):
    coa.overrides["serve"] = httpx.Response(status, text="<html>" + "x" * 5000)
    ((kind, data),) = events(_ask(client).text)
    assert (kind, data["code"], data["status"]) == ("error", code, status)
    assert "<html>" not in json.dumps(data)


def test_a_non_streamed_json_answer_is_read_bounded_as_one_done(client, account, coa, monkeypatch):
    coa.overrides["serve"] = httpx.Response(200, json={
        "result": {"tier": 1, "synthesizedAnswer": "41", "confidence": {"score": 1}},
        "sessionId": "s-json",
    })
    ((kind, data),) = events(_ask(client).text)
    assert (kind, data["answer"], data["session_id"]) == ("done", "41", "s-json")
    # a body past the bound is cut, not read whole: it no longer parses, so no answer
    monkeypatch.setattr(pg, "BODY_MAX", 16)
    ((kind, data),) = events(_ask(client).text)
    assert (kind, data["code"]) == ("error", "context_layer.upstream")


def test_a_stream_that_ends_without_done_is_an_upstream_error(client, account, coa):
    coa.serve_frames = fx.SERVE_FRAMES[:2]
    kinds = events(_ask(client).text)
    assert [k for k, _ in kinds] == ["step", "step", "error"]
    assert kinds[-1][1]["code"] == "context_layer.upstream"


def test_an_unreachable_or_slow_coa_is_an_upstream_error(client, account, coa):
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    coa.overrides["serve"] = timeout
    ((kind, data),) = events(_ask(client).text)
    assert (kind, data["code"]) == ("error", "context_layer.upstream")


def test_nothing_connected_and_bad_input_answer_before_any_stream(client, monkeypatch, coa):
    fx.install(monkeypatch)
    response = _ask(client)
    assert response.status_code == 409
    assert response.json()["code"] == "context_layer.not_connected"
    for bad in ({"namespace_id": "nope"}, {"query": ""}, {"mode": "turbo"},
                {"session_id": "a b"}):
        assert client.post(URL, json={"namespace_id": fx.NAMESPACE, "query": "q",
                                      **bad}).status_code == 422
    assert coa.requests == []


# ── stream lifetime (driven directly) ────────────────────────────────────────


class SlowStream(httpx.AsyncByteStream):
    """An SSE body that sends ``chunks`` with ``gap`` seconds between them and
    records whether httpx closed it."""

    def __init__(self, chunks: list[bytes], gap: float):
        self.chunks, self.gap, self.closed, self.sent = chunks, gap, False, 0

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.sent:
                await asyncio.sleep(self.gap)
            self.sent += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _layer() -> context_layer.ContextLayer:
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        return context_layer.require(db, "default")
    finally:
        db.close()


def _drive(monkeypatch, body: SlowStream, *, stop_after: int, heartbeat_s=15.0,
           is_disconnected=None) -> list[str]:
    monkeypatch.setattr(pg, "_transport", lambda: httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body)
    ))
    layer = _layer()

    async def run() -> list[str]:
        out: list[str] = []
        gen = pg.stream(
            layer=layer, token="t", workspace_id="default", username="alice",
            namespace_id=fx.NAMESPACE, query="q", heartbeat_s=heartbeat_s,
            is_disconnected=is_disconnected,
        )
        async for chunk in gen:
            out.append(chunk)
            if len(out) >= stop_after:
                break
        await gen.aclose()  # what Starlette does when the console goes away
        return out

    return asyncio.run(run())


def _chunks(frames) -> list[bytes]:
    return [fx.sse_body([frame]) for frame in frames]


def test_closing_the_stream_early_closes_the_upstream(monkeypatch, account):
    body = SlowStream(_chunks(fx.SERVE_FRAMES), gap=0.05)
    out = _drive(monkeypatch, body, stop_after=1)
    assert out[0].startswith("event: step")
    assert body.closed is True and body.sent < len(fx.SERVE_FRAMES)


def test_a_client_disconnect_stops_reading_coa(monkeypatch, account):
    gone = [False]

    async def is_disconnected() -> bool:
        return gone[0]

    body = SlowStream(_chunks(fx.SERVE_FRAMES), gap=0.02)

    async def run():
        monkeypatch.setattr(pg, "_transport", lambda: httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=body)
        ))
        out = []
        async for chunk in pg.stream(
            layer=_layer(), token="t", workspace_id="default", username="alice",
            namespace_id=fx.NAMESPACE, query="q", is_disconnected=is_disconnected,
        ):
            out.append(chunk)
            gone[0] = True  # the console left after the first event
        return out

    out = asyncio.run(run())
    assert len(out) == 1 and body.closed is True


def test_a_silent_coa_gets_keep_alive_comments_and_the_stream_continues(monkeypatch, account):
    body = SlowStream(_chunks(fx.SERVE_FRAMES), gap=0.06)
    out = _drive(monkeypatch, body, stop_after=50, heartbeat_s=0.01)
    assert pg.KEEP_ALIVE in out
    kinds = [c.split("\n", 1)[0] for c in out if c != pg.KEEP_ALIVE]
    assert kinds == ["event: step", "event: step", "event: token", "event: done"]
    assert body.closed is True
