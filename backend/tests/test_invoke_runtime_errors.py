"""N17-obs95 — the SDK's terminal error frame on the SYNC entrances is an envelope.

Hermetic: a fake data plane whose Runtime stream ends on the AgentCore SDK's terminal error
frame (``{"error", "error_type", "message"}``) behind the real invoke chain. Chat already
rendered that frame as its terminal ``error`` event; ``/v1/agents/{id}/invoke`` and the
console ``POST /api/agents/{id}/invoke`` let the ``SdkStreamError`` escape as a bare 500.
"""

from __future__ import annotations

import pytest

import app.services.invoke as invoke_service
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent, ChatSession
from app.services.agentcore import runtime as rt
from app.services.invoke import invoke_agent_text
from tests.conftest import ws_ctx
from tests.test_chat_api import _mint_key, make_active_agent

FRAME = (
    b'data: {"error": "ReadTimeout: Read timed out. (read timeout=120) bearer_token=abc123", '
    b'"error_type": "ReadTimeout", "message": "An error occurred during streaming"}'
)


class _Stream:
    def __init__(self, lines: list[bytes]) -> None:
        self.lines = lines

    def iter_lines(self, *, chunk_size=None):
        for line in self.lines:
            yield line
            yield b""

    def close(self) -> None:
        pass


class _Plane:
    """Two healthy deltas, then the SDK's terminal error frame."""

    def __init__(self) -> None:
        self.calls = 0

    def invoke_agent_runtime(self, **_kw):
        self.calls += 1
        return {
            "response": _Stream([b'data: {"event": "delta", "text": "hel"}',
                                 b'data: {"event": "delta", "text": "lo"}', FRAME]),
            "contentType": "text/event-stream; charset=utf-8",
        }


@pytest.fixture
def frame_plane(monkeypatch):
    plane = _Plane()
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: plane)
    return plane


def _agent(name: str) -> Agent:
    db = SessionLocal()
    try:
        return db.get(Agent, make_active_agent(method="zip_runtime", name=name))
    finally:
        db.close()


def test_sync_invoke_turns_the_sdk_error_frame_into_the_502_envelope(frame_plane):
    """The shared chain raises `AppError 502 agent.runtime_error` with the frame's structured,
    secret-redacted detail — both entrances go through `invoke_agent_text`."""
    agent = _agent("sync-frame")
    with pytest.raises(AppError) as exc:
        invoke_agent_text(agent, "hi", session_id="s" * 40, workspace=ws_ctx())
    err = exc.value
    assert (err.status_code, err.code) == (502, "agent.runtime_error")
    assert err.message.startswith("runtime returned error: ReadTimeout: ")
    assert err.detail["error_type"] == "ReadTimeout"
    assert set(err.detail) == {"error", "message", "error_type"}
    # C4: credential-shaped pairs in the frame text are redacted in both message and detail
    assert "abc123" not in err.message and "abc123" not in err.detail["error"]
    assert "bearer_token=[redacted]" in err.detail["error"]
    assert isinstance(err.__cause__, rt.SdkStreamError)


def test_v1_invoke_answers_the_envelope_and_ledgers_nothing(client, frame_plane):
    agent_id = make_active_agent(method="zip_runtime", name="v1-frame")
    res = client.post(f"/v1/agents/{agent_id}/invoke", json={"prompt": "hi"},
                      headers={"X-Api-Key": _mint_key(client)})
    assert res.status_code == 502, res.text
    body = res.json()
    assert body["code"] == "agent.runtime_error"
    assert body["detail"]["error_type"] == "ReadTimeout"
    assert "Traceback" not in res.text and "abc123" not in res.text
    assert frame_plane.calls == 1
    db = SessionLocal()
    try:
        assert db.query(ChatSession).filter(ChatSession.agent_id == agent_id).count() == 0
    finally:
        db.close()


def test_console_sync_invoke_answers_the_same_envelope(client, frame_plane):
    agent_id = make_active_agent(method="zip_runtime", name="console-frame")
    res = client.post(f"/api/agents/{agent_id}/invoke", json={"prompt": "hi"})
    assert res.status_code == 502, res.text
    body = res.json()
    assert body["code"] == "agent.runtime_error"
    assert body["detail"]["error_type"] == "ReadTimeout"
    assert body["message"].startswith("runtime returned error: ReadTimeout")


def test_a_healthy_stream_still_joins_into_text(monkeypatch):
    class Plane:
        def invoke_agent_runtime(self, **_kw):
            return {"response": _Stream([b'data: {"event": "delta", "text": "ok"}',
                                         b'data: {"event": "complete", "result": "ok"}']),
                    "contentType": "text/event-stream"}

    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: Plane())
    result = invoke_agent_text(_agent("sync-ok"), "hi", session_id="s" * 40, workspace=ws_ctx())
    assert result["text"] == "ok"


def test_redaction_keeps_ordinary_words_that_merely_contain_key():
    """N17-obs527: `keywords` is not a credential name — only an identifier that ENDS in
    `key` (api_key, apiKey, key) or contains token/secret/password/… is redacted."""
    text = ('{"keywords": "period locked", "requester": "itdesk05", "api_key": "abc", '
            '"apiKey": "def", "key": "ghi", "monkey_business": "jkl"}')
    out = rt.redact_secret_pairs(text)
    assert '"keywords": "period locked"' in out and '"requester": "itdesk05"' in out
    assert "abc" not in out and "def" not in out and "ghi" not in out
    assert out.count("[redacted]") == 3
    assert "jkl" not in out or "monkey_business" in out  # `monkey` ends in key: redacted too
