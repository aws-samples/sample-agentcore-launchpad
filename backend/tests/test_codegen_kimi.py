"""Studio AI-fix backend: Kimi K3 over Global Bedrock's OpenAI-compatible endpoint."""

import asyncio
import io
import json
import urllib.error

import pytest

from app.codegen import config
from app.codegen.backends import kimi, registry
from app.codegen.backends.base import GenerationError, GenerationTask
from app.core.config import get_settings


def test_kimi_is_the_default_and_only_registered_backend():
    assert get_settings().codegen_backend == "kimi"
    assert get_settings().codegen_model == "global.moonshotai.kimi-k3"
    assert registry.available_backends() == ["kimi"]
    assert isinstance(registry.get_backend(), kimi.KimiBackend)


def test_check_available_needs_the_bearer(monkeypatch):
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    ok, reason = asyncio.run(kimi.KimiBackend().check_available())
    assert ok is False and "AWS_BEARER_TOKEN_BEDROCK" in reason
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "k")
    assert asyncio.run(kimi.KimiBackend().check_available()) == (True, "")


def _workspace(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("task brief", encoding="utf-8")
    (tmp_path / "contract_spec.md").write_text("rule 1", encoding="utf-8")
    (tmp_path / "flow.json").write_text("{}", encoding="utf-8")
    (tmp_path / "error.txt").write_text("Traceback: boom", encoding="utf-8")
    (tmp_path / config.GENERATED_FILENAME).write_text("print('old')\n", encoding="utf-8")
    return tmp_path


class _Response:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _reply(content) -> dict:
    # Kimi answers with reasoning_content too — only `content` is read (spec C3)
    return {"choices": [{"message": {"reasoning_content": "thinking…", "content": content}}]}


def test_generate_posts_chat_completions_with_bearer_and_writes_both_artifacts(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bedrock-api-key")
    captured: dict = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _Response(_reply(json.dumps({
            "diagnosis": {"category": "code", "summary": "off by one", "suggestions": []},
            "generated_agent": "print('new')\n",
        })))

    monkeypatch.setattr(kimi.urllib.request, "urlopen", fake_urlopen)
    progress: list[str] = []

    async def on_progress(message: str) -> None:
        progress.append(message)

    workspace = _workspace(tmp_path)
    asyncio.run(kimi.KimiBackend().generate(
        workspace, GenerationTask(flow_data={}, mode="fix"), on_progress
    ))

    assert captured["url"] == (
        "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1/chat/completions"
    )
    assert captured["headers"]["Authorization"] == "Bearer bedrock-api-key"
    assert captured["headers"]["Content-type"] == "application/json"
    body = captured["body"]
    assert body["model"] == "global.moonshotai.kimi-k3"
    assert body["max_tokens"] >= 256
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    user = body["messages"][1]["content"]
    assert "### error.txt" in user and "Traceback: boom" in user and "print('old')" in user
    assert json.loads((workspace / "diagnosis.json").read_text())["category"] == "code"
    assert (workspace / config.GENERATED_FILENAME).read_text() == "print('new')\n"
    assert any("corrected" in p for p in progress)


def test_repair_round_sends_the_validation_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "k")
    captured: dict = {}

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data)
        return _Response(_reply('{"diagnosis": {"category": "code", "summary": "s"}, '
                                '"generated_agent": "print(2)\\n"}'))

    monkeypatch.setattr(kimi.urllib.request, "urlopen", fake_urlopen)
    task = GenerationTask(flow_data={}, mode="fix", previous_code="print(1)",
                          validation_errors=["missing main guard"])
    asyncio.run(kimi.KimiBackend().generate(_workspace(tmp_path), task, _noop))
    assert "missing main guard" in captured["body"]["messages"][1]["content"]


async def _noop(_message: str) -> None:
    return None


def test_a_diagnosis_without_code_leaves_the_file_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "k")
    monkeypatch.setattr(
        kimi.urllib.request, "urlopen",
        lambda request, timeout: _Response(_reply(
            "```json\n{\"diagnosis\": {\"category\": \"environment\", \"summary\": "
            "\"no key\"}, \"generated_agent\": null}\n```"
        )),
    )
    workspace = _workspace(tmp_path)
    asyncio.run(kimi.KimiBackend().generate(
        workspace, GenerationTask(flow_data={}, mode="fix"), _noop
    ))
    assert (workspace / config.GENERATED_FILENAME).read_text() == "print('old')\n"
    assert json.loads((workspace / "diagnosis.json").read_text())["category"] == "environment"


def test_http_and_shape_failures_become_generation_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "k")

    def http_400(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 400, "bad", {}, io.BytesIO(b"nope"))

    monkeypatch.setattr(kimi.urllib.request, "urlopen", http_400)
    with pytest.raises(GenerationError, match="HTTP 400"):
        asyncio.run(kimi.KimiBackend().generate(
            _workspace(tmp_path), GenerationTask(flow_data={}, mode="fix"), _noop
        ))
    monkeypatch.setattr(kimi.urllib.request, "urlopen",
                        lambda request, timeout: _Response(_reply("not json at all")))
    with pytest.raises(GenerationError, match="JSON"):
        asyncio.run(kimi.KimiBackend().generate(
            _workspace(tmp_path), GenerationTask(flow_data={}, mode="fix"), _noop
        ))


def test_generate_raises_generation_error_when_bearer_vanishes_before_the_post(
    tmp_path, monkeypatch
):
    """check_available() passed, but the var is gone by the time generate() posts —
    surfaces as a legible GenerationError, never a raw KeyError."""
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "k")
    backend = kimi.KimiBackend()
    assert asyncio.run(backend.check_available()) == (True, "")
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)

    def fail_if_called(request, timeout):
        raise AssertionError("urlopen must not be reached without a bearer")

    monkeypatch.setattr(kimi.urllib.request, "urlopen", fail_if_called)
    with pytest.raises(GenerationError, match="AWS_BEARER_TOKEN_BEDROCK"):
        asyncio.run(backend.generate(
            _workspace(tmp_path), GenerationTask(flow_data={}, mode="fix"), _noop
        ))


def test_parse_reply_shapes():
    diagnosis, code = kimi.parse_reply(
        '{"diagnosis": {"category": "code"}, "generated_agent": "x"}'
    )
    assert diagnosis == {"category": "code"} and code == "x"
    diagnosis, code = kimi.parse_reply('prose {"diagnosis": {}, "generated_agent": "  "} prose')
    assert diagnosis == {} and code is None
