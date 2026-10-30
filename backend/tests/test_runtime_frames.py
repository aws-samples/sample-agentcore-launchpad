"""N17-obs96 / N17-obs97 (/ N17-obs114) — what a Runtime answer may never be rendered as.

Hermetic: the shared normaliser (`rt._runtime_payload_events` / `_normalized_runtime_events`)
and the real chat chain over a fake data plane. The recorded live shapes
(`tests/fixtures/seams/agentcore-runtime/*`): the agent template's error frame cut at 300
characters (a connection dropped mid-frame) and the evaluation worker's REJECTED ack body.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.services.invoke as invoke_service
from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.services.agentcore import runtime as rt
from app.services.chat import chat_stream
from tests.conftest import ws_ctx
from tests.test_chat_api import make_active_agent

SEAMS = Path(__file__).parent / "fixtures" / "seams" / "agentcore-runtime"
PARTIAL_LINE = json.loads((SEAMS / "invoke_stream_success.json").read_text("utf-8"))[
    "response_or_error"]["first_3_lines"][0]
REJECTED_BODY = json.loads((SEAMS / "eval_worker_invalid_job_payload.json").read_text("utf-8"))[
    "response_or_error"]["first_3_lines"][0]
HEALTHY = [b'data: {"event": "delta", "text": "hel"}', b'data: {"event": "delta", "text": "lo"}']


class _Stream:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines

    def iter_lines(self, *, chunk_size=None):
        for line in self.lines:
            yield line
            yield b""

    def close(self) -> None:
        pass


class _Body:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def read(self) -> bytes:
        return self.raw

    def close(self) -> None:
        pass


def _sse_plane(lines: list[bytes]):
    class Plane:
        def invoke_agent_runtime(self, **_kw):
            return {"response": _Stream(lines), "contentType": "text/event-stream; charset=utf-8"}

    return Plane()


def _json_plane(raw: bytes, content_type: str = "application/json"):
    class Plane:
        def invoke_agent_runtime(self, **_kw):
            return {"response": _Body(raw), "contentType": content_type}

    return Plane()


def _agent(name: str) -> Agent:
    db = SessionLocal()
    try:
        return db.get(Agent, make_active_agent(method="zip_runtime", name=name))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# the normaliser
# ---------------------------------------------------------------------------


def test_a_non_json_data_line_is_a_malformed_frame_never_reply_text():
    """N17-obs96: `_sse_payloads` marks the cut line; the parser raises the SDK-shaped error
    with the cut text bounded in `detail.error`."""
    (payload,) = list(rt._sse_payloads([PARTIAL_LINE.encode("utf-8"), b""]))
    assert isinstance(payload, rt.MalformedFrame) and isinstance(payload, str)
    with pytest.raises(rt.SdkStreamError) as exc:
        list(rt._runtime_payload_events(payload))
    assert exc.value.error_type == rt.MALFORMED_FRAME_TYPE
    assert exc.value.detail["error"].startswith('{"error": "no workload access token')
    assert exc.value.detail["message"] == rt.MALFORMED_FRAME_MESSAGE
    assert not rt.is_upstream_transient(exc.value)  # not a retry-worthy upstream hiccup


def test_a_partial_frame_after_healthy_deltas_surfaces_as_an_error_not_silently_dropped():
    """N17-obs96, the tail variant: the deltas are yielded first (the partial answer is kept —
    assistant-turns.md §8.2), then the cut line raises instead of vanishing."""
    lines = [*HEALTHY, PARTIAL_LINE.encode("utf-8")]
    out: list[dict] = []
    gen = rt._normalized_runtime_events(rt._sse_payloads(_Stream(lines).iter_lines()))
    with pytest.raises(rt.SdkStreamError) as exc:
        for event in gen:
            out.append(event)
    assert [e["data"]["text"] for e in out] == ["hel", "lo"]
    assert exc.value.error_type == rt.MALFORMED_FRAME_TYPE


def test_a_rejected_json_body_is_an_error_not_the_free_form_reply():
    """N17-obs97: `code` + `state` is a structured refusal; the `message` key no longer wins."""
    body = json.loads(REJECTED_BODY)
    assert rt.is_rejection_body(body)
    with pytest.raises(rt.SdkStreamError) as exc:
        list(rt._runtime_payload_events(body))
    assert exc.value.error_type == rt.REJECTED_BODY_TYPE
    assert exc.value.detail["code"] == "evaluation.kind_unsupported"
    assert exc.value.detail["state"] == "REJECTED"
    assert "unknown job_type" in exc.value.detail["error"]
    # `code` + a falsy `error` key is a refusal too; `code` alone is not
    assert rt.is_rejection_body({"code": "x", "error": None})
    assert not rt.is_rejection_body({"code": "x", "message": "m"})
    assert not rt.is_rejection_body({"code": "", "state": "REJECTED"})


@pytest.mark.parametrize(
    ("payload", "text"),
    [
        ({"answer": "forty-two"}, "forty-two"),  # the devguide shape
        ({"result": "done"}, "done"),  # BYOC / BedrockAgentCoreApp default
        ("a bare JSON string frame", "a bare JSON string frame"),  # the SDK's yield "text"
        ({"response": {"text": "nested"}, "code": "ok"}, "nested"),  # `code` without state/error
    ],
)
def test_legitimate_free_form_bodies_still_render(payload, text):
    assert list(rt._runtime_payload_events(payload)) == [
        {"event": "complete", "data": {"text": text}}]


def test_a_plain_text_non_sse_body_still_renders_as_the_reply():
    """Only an SSE `data:` line is held to the JSON protocol; a `text/plain` body is text."""
    events = list(rt._runtime_body_events(_Body(b"plain words"), "text/plain", False))
    assert events == [{"event": "delta", "data": {"text": "plain words"}}]


# ---------------------------------------------------------------------------
# the chat chain end to end
# ---------------------------------------------------------------------------


def test_chat_ends_on_error_when_the_whole_response_is_one_cut_line(monkeypatch):
    monkeypatch.setattr(invoke_service, "data_client",
                        lambda _ws=None: _sse_plane([PARTIAL_LINE.encode("utf-8")]))
    events = list(chat_stream(_agent("cut-alone"), "hi", session_id="s" * 40, workspace=ws_ctx()))
    assert [e["event"] for e in events] == ["meta", "error"]
    data = events[-1]["data"]
    assert "SdkStreamError" in data["message"] and "malformed runtime frame" in data["message"]
    assert data["detail"]["error_type"] == rt.MALFORMED_FRAME_TYPE  # N17-obs98: §4 parity


def test_chat_keeps_the_deltas_and_ends_on_error_when_the_cut_line_follows_them(monkeypatch):
    monkeypatch.setattr(invoke_service, "data_client",
                        lambda _ws=None: _sse_plane([*HEALTHY, PARTIAL_LINE.encode("utf-8")]))
    events = list(chat_stream(_agent("cut-tail"), "hi", session_id="s" * 40, workspace=ws_ctx()))
    assert [e["event"] for e in events] == ["meta", "delta", "delta", "error"]
    assert "".join(e["data"]["text"] for e in events if e["event"] == "delta") == "hello"


def test_chat_ends_on_error_for_the_rejected_json_body(monkeypatch):
    monkeypatch.setattr(invoke_service, "data_client",
                        lambda _ws=None: _json_plane(REJECTED_BODY.encode("utf-8")))
    events = list(chat_stream(_agent("rejected"), "hi", session_id="s" * 40, workspace=ws_ctx()))
    assert [e["event"] for e in events] == ["meta", "error"]
    data = events[-1]["data"]
    assert rt.REJECTED_BODY_TYPE in data["message"]
    assert data["detail"]["error_type"] == rt.REJECTED_BODY_TYPE  # N17-obs98: §4 parity
    assert data["detail"]["code"] == "evaluation.kind_unsupported"
    assert "unknown job_type" not in "".join(
        e["data"].get("text", "") for e in events if e["event"] == "delta")


# ---------------------------------------------------------------------------
# N17-obs114 — a runtime that completes with EMPTY text is an error, never silence
# ---------------------------------------------------------------------------

from app.core.errors import AppError  # noqa: E402
from tests.test_chat_api import _mint_key  # noqa: E402


def _drain(gen):
    out: list[dict] = []
    with pytest.raises(AppError) as exc:
        for event in gen:
            out.append(event)
    return out, exc.value


@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (b'{"result": ""}', "application/json"),  # the recorded sample body
        (b'data: {"event": "complete", "result": ""}', "text/event-stream"),
        (b'data: {"event": "heartbeat"}\n\ndata: {"event": "complete", "result": ""}',
         "text/event-stream"),
        (b"", "text/event-stream"),  # a stream that ended with no frame at all
        (b"", "application/json"),
        (b'data: {"event": "truncated", "truncated": false, "stop_reason": "max_tokens", '
         b'"continuations": 1}\n\ndata: {"event": "complete", "result": ""}',
         "text/event-stream"),  # a textless continuation that ended normally
    ],
)
def test_an_empty_answer_raises_agent_empty_answer(body, content_type):
    out, err = _drain(rt._runtime_body_events(_Body(body), content_type, False))
    assert [e["event"] for e in out if e["event"] != "heartbeat"] in ([], ["truncated"])
    assert (err.status_code, err.code) == (502, "agent.empty_answer")
    assert err.detail == {"reason": "empty_answer"}


@pytest.mark.parametrize(
    "lines",
    [
        [b'data: {"event": "auth_required", "provider": "github", "tool": "repos", '
         b'"url": "https://idp.example/authorize"}', b'data: {"event": "complete", "result": ""}'],
        [b'data: {"event": "tool", "name": "calculator", "id": "t1"}',
         b'data: {"event": "complete", "result": ""}'],
        [b'data: {"event": "policy_denied", "tool": "x", "reason": "forbidden"}',
         b'data: {"event": "complete", "result": ""}'],
        [b'data: {"event": "delta", "text": "notice"}',
         b'data: {"event": "truncated", "truncated": true, "stop_reason": "max_tokens", '
         b'"continuations": 0}', b'data: {"event": "complete", "result": "notice", '
         b'"truncated": true}'],
    ],
)
def test_a_turn_that_rendered_a_card_or_notice_is_not_empty(lines):
    events = list(rt._runtime_body_events(_Stream(lines), "text/event-stream", False))
    assert events and all(e["event"] != "error" for e in events)


def test_flatten_sse_text_keeps_its_none_for_an_empty_stream():
    """The converter's helper reads recorded text, not a live turn: no answer is `None`."""
    assert rt.flatten_sse_text('data: {"event": "complete", "result": ""}\n') is None


def test_chat_ends_on_an_error_row_for_the_empty_answer(monkeypatch):
    monkeypatch.setattr(invoke_service, "data_client",
                        lambda _ws=None: _json_plane(b'{"result": ""}'))
    events = list(chat_stream(_agent("empty-chat"), "hi", session_id="s" * 40, workspace=ws_ctx()))
    assert [e["event"] for e in events] == ["meta", "error"]
    assert events[-1]["data"]["code"] == "agent.empty_answer"
    assert events[-1]["data"]["detail"] == {"reason": "empty_answer"}


def test_v1_sync_invoke_answers_502_agent_empty_answer(client, monkeypatch):
    monkeypatch.setattr(invoke_service, "data_client",
                        lambda _ws=None: _json_plane(b'{"result": ""}'))
    agent_id = make_active_agent(method="zip_runtime", name="empty-v1")
    res = client.post(f"/v1/agents/{agent_id}/invoke", json={"prompt": "hi"},
                      headers={"X-Api-Key": _mint_key(client)})
    assert res.status_code == 502, res.text
    assert res.json()["code"] == "agent.empty_answer"


# ---------------------------------------------------------------------------
# N17-obs98 — the chat `error` event carries the SDK frame's detail (§4 parity)
# ---------------------------------------------------------------------------

SDK_FRAME = (b'data: {"error": "ModelThrottledException: Too many requests", '
             b'"error_type": "ModelThrottledException", '
             b'"message": "An error occurred during streaming"}')


def test_chat_error_event_for_the_sdk_frame_carries_detail_error_type(monkeypatch):
    """assistant-turns.md §8.2 "Terminal `error` data — §4 parity": `{message, detail}` with the
    frame's `error_type`, the partial answer kept, exactly as the architect turn."""
    monkeypatch.setattr(invoke_service, "data_client",
                        lambda _ws=None: _sse_plane([*HEALTHY, SDK_FRAME]))
    events = list(chat_stream(_agent("frame-detail"), "hi", session_id="s" * 40,
                              workspace=ws_ctx()))
    assert [e["event"] for e in events] == ["meta", "delta", "delta", "error"]
    data = events[-1]["data"]
    assert data["message"].startswith("SdkStreamError: runtime returned error: "
                                      "ModelThrottledException")
    assert data["detail"] == {"error": "ModelThrottledException: Too many requests",
                              "message": "An error occurred during streaming",
                              "error_type": "ModelThrottledException"}
    assert "code" not in data  # not an AppError: the §4 `{message, detail}` shape


def test_a_free_form_body_with_a_benign_state_is_not_a_rejection():
    """rev-r17 on cn/r17-o: ``code`` + any string ``state`` was too wide a net — only a refusal
    vocabulary (REJECTED / FAILED / ERROR / DENIED / REFUSED) or an ``error`` key qualifies."""
    from app.services.agentcore import runtime as rt

    assert not rt.is_rejection_body({"code": "OK", "state": "done", "message": "all good"})
    assert rt.is_rejection_body({"code": "E42", "state": "rejected", "message": "no"})
    assert rt.is_rejection_body({"code": "E42", "error": "boom"})
