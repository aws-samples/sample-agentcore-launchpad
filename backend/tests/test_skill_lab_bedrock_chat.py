"""Unit tests for the vendored bedrock_chat backend (LAUNCHPAD PATCH #1).

The vendored tree is never imported by backend/app code (subprocess-only rule);
tests may load the single module under test, but must not execute
skillopt.model.__init__ (it imports openai, absent from this venv). Stub parent
packages make `skillopt.model.common` / `.bedrock_chat` importable alone.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

VENDOR_PKG = Path(__file__).resolve().parents[2] / "vendor" / "skillopt" / "skillopt"


@pytest.fixture(scope="module")
def bedrock_chat():
    saved = {name: sys.modules.get(name) for name in
             ("skillopt", "skillopt.model", "skillopt.model.common",
              "skillopt.model.bedrock_chat")}
    pkg = types.ModuleType("skillopt")
    pkg.__path__ = [str(VENDOR_PKG)]
    model_pkg = types.ModuleType("skillopt.model")
    model_pkg.__path__ = [str(VENDOR_PKG / "model")]
    sys.modules["skillopt"] = pkg
    sys.modules["skillopt.model"] = model_pkg
    saved_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # keep __pycache__ out of the vendored tree
    try:
        module = importlib.import_module("skillopt.model.bedrock_chat")
        yield module
    finally:
        sys.dont_write_bytecode = saved_bytecode
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


class FakeBedrockClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def converse(self, **request):
        self.calls.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok_payload(text="FACT: ok", inp=11, out=7):
    return {
        "output": {"message": {"content": [{"text": text}]}},
        "usage": {"inputTokens": inp, "outputTokens": out, "totalTokens": inp + out},
        "stopReason": "end_turn",
    }


def _throttle(module_exc_name="ThrottlingException"):
    exc = Exception("throttled")
    exc.response = {"Error": {"Code": module_exc_name}}
    return exc


@pytest.fixture
def fake_client(bedrock_chat, monkeypatch):
    holder = {}

    def install(responses):
        client = FakeBedrockClient(responses)
        monkeypatch.setattr(bedrock_chat, "_client", lambda region, timeout: client)
        holder["client"] = client
        return client

    return install


def test_chat_optimizer_maps_converse(bedrock_chat, fake_client, monkeypatch):
    client = fake_client([_ok_payload()])
    monkeypatch.setattr(bedrock_chat.tracker, "_data", {}, raising=False)
    text, usage = bedrock_chat.chat_optimizer(system="SYS", user="USR", stage="judge")
    assert text == "FACT: ok"
    assert usage == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    request = client.calls[0]
    assert request["system"] == [{"text": "SYS"}]
    assert request["messages"] == [{"role": "user", "content": [{"text": "USR"}]}]
    assert request["inferenceConfig"]["maxTokens"] == 16384
    assert bedrock_chat.get_token_summary()["judge"]["calls"] == 1


def test_retries_on_throttle_then_succeeds(bedrock_chat, fake_client, monkeypatch):
    monkeypatch.setattr(bedrock_chat.time, "sleep", lambda _s: None)
    client = fake_client([_throttle(), _ok_payload(text="second try")])
    text, _usage = bedrock_chat.chat_optimizer(system="S", user="U", retries=3)
    assert text == "second try"
    assert len(client.calls) == 2


def test_non_retryable_error_raises(bedrock_chat, fake_client):
    exc = Exception("denied")
    exc.response = {"Error": {"Code": "AccessDeniedException"}}
    fake_client([exc])
    with pytest.raises(Exception, match="denied"):
        bedrock_chat.chat_optimizer(system="S", user="U", retries=3)


def test_empty_response_retries_then_raises(bedrock_chat, fake_client, monkeypatch):
    monkeypatch.setattr(bedrock_chat.time, "sleep", lambda _s: None)
    empty = _ok_payload(text="")
    empty["output"]["message"]["content"] = []
    fake_client([empty, empty])
    with pytest.raises(RuntimeError, match="empty response"):
        bedrock_chat.chat_optimizer(system="S", user="U", retries=2)


def test_tools_are_refused(bedrock_chat):
    with pytest.raises(NotImplementedError):
        bedrock_chat.chat_optimizer_messages(
            [{"role": "user", "content": "x"}], tools=[{"name": "t"}]
        )


def test_deployment_setter_and_default(bedrock_chat):
    bedrock_chat.set_optimizer_deployment("")
    assert bedrock_chat.OPTIMIZER_CONFIG.deployment == "global.anthropic.claude-opus-5"
    bedrock_chat.set_optimizer_deployment("us.anthropic.claude-sonnet-5")
    assert bedrock_chat.OPTIMIZER_CONFIG.deployment == "us.anthropic.claude-sonnet-5"


def test_multiline_and_system_merge(bedrock_chat, fake_client):
    client = fake_client([_ok_payload()])
    bedrock_chat.chat_target_messages(
        [
            {"role": "system", "content": "A"},
            {"role": "user", "content": [{"text": "line1"}, {"text": "line2"}]},
        ]
    )
    request = client.calls[0]
    assert request["system"] == [{"text": "A"}]
    assert request["messages"][0]["content"] == [{"text": "line1\nline2"}]
