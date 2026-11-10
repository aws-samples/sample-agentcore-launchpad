"""N17-obs547 — a judge / reflection call on an OpenAI-compatible provider STREAMS.

The CN OpenAI-compatible providers can sit behind a response-timeout proxy (`litellm` behind
CloudFront: the origin response timeout is a between-bytes timeout, so a streamed completion
is never cut by it while a single-body request longer than the timeout is a 504). This module
pins what the hub's `aws_clients.build_chat_model` and the worker's `judge_model` (its twin,
`test_eval_worker_template`) hand strands: an ``OpenAIModel`` whose every Agent turn —
the strands-evals evaluators' tool-based ``structured_output_model`` judgement and
``jobs_recommend.model_converse``'s text reflection alike — issues ONE
``chat.completions.create(stream=True, stream_options={"include_usage": True})`` request and
accumulates the deltas into the same text / usage a single-body answer would carry.

The only switch is the catalog row's ``streaming`` flag (``nostream`` trait / probe): a row
that says the endpoint cannot stream keeps the single-body request — documented here, not
overridden. The client bounds (``judge_client``: 300 s per request, one SDK retry) are the
Round 3 ones: behind a proxy whose own timeout is shorter, the proxy's 504 arrives first as
``openai.InternalServerError`` and the engine's classified transient retry handles it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel
from strands import Agent
from strands.models.openai import OpenAIModel

from app.services import aws_clients
from app.services.strands_eval import jobs_recommend, judge_client

SF = "siliconflow.zai-org/GLM-5.3"
pytestmark = pytest.mark.siliconflow


class Rating(BaseModel):
    score: float
    reasoning: str


# ─── a stand-in for ``openai.AsyncOpenAI`` ─────────────────────────────────────
class _Usage:
    prompt_tokens = 7
    completion_tokens = 3
    total_tokens = 10
    prompt_tokens_details = None


class _Fn:
    def __init__(self, name: str | None, arguments: str | None) -> None:
        self.name, self.arguments = name, arguments


class _ToolCall:
    def __init__(self, index: int, id: str | None, name: str | None, arguments: str | None
                 ) -> None:
        self.index, self.id, self.type = index, id, "function"
        self.function = _Fn(name, arguments)


class _Delta:
    def __init__(self, content: str | None = None, reasoning_content: str | None = None,
                 tool_calls: list[_ToolCall] | None = None) -> None:
        self.content, self.reasoning_content = content, reasoning_content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, delta: Any, finish_reason: str | None = None) -> None:
        self.delta, self.finish_reason = delta, finish_reason
        self.message = delta  # the single-body shape reads `.message` instead of `.delta`


class _Chunk:
    def __init__(self, choices: list[_Choice], usage: Any = None) -> None:
        self.choices, self.usage = choices, usage


RATING = {"score": 0.9, "reasoning": "complete and correct"}
TEXT_PARTS = ("The prompt should ", "name the escalation path.")


class _Completions:
    """Answers a tool request with a streamed tool call (the structured-output tool) and a
    plain request with two text deltas; a ``stream=False`` request gets the single-body
    equivalent. Records every request body."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def create(self, **request: Any) -> Any:
        self.requests.append(request)
        tools = request.get("tools") or []
        tool_name = tools[0]["function"]["name"] if tools else None
        arguments = json.dumps(RATING) if tool_name else None
        if not request.get("stream"):
            message = _Delta(
                content=None if tool_name else "".join(TEXT_PARTS),
                reasoning_content="weighing the transcript",
                tool_calls=[_ToolCall(0, "call_1", tool_name, arguments)] if tool_name else None,
            )
            return _Chunk([_Choice(message, "tool_calls" if tool_name else "stop")], _Usage())

        async def stream() -> Any:
            yield _Chunk([_Choice(_Delta(reasoning_content="weighing "))])
            yield _Chunk([_Choice(_Delta(reasoning_content="the transcript"))])
            if tool_name:
                half = len(arguments) // 2
                for call in (_ToolCall(0, "call_1", tool_name, ""),
                             _ToolCall(0, None, None, arguments[:half]),
                             _ToolCall(0, None, None, arguments[half:])):
                    yield _Chunk([_Choice(_Delta(tool_calls=[call]))])
                yield _Chunk([_Choice(_Delta(), "tool_calls")])
            else:
                for part in TEXT_PARTS:
                    yield _Chunk([_Choice(_Delta(content=part))])
                yield _Chunk([_Choice(_Delta(), "stop")])
            yield _Chunk([], _Usage())  # the usage-only frame `include_usage` adds

        return stream()


class _Chat:
    def __init__(self) -> None:
        self.completions = _Completions()


class StubAsyncOpenAI:
    """``openai.AsyncOpenAI`` as strands uses it: constructed per request from
    ``client_args`` and entered as an async context manager."""

    constructed: list[dict[str, Any]] = []
    completions = _Completions()

    def __init__(self, **kwargs: Any) -> None:
        type(self).constructed.append(dict(kwargs))
        self.chat = _Chat()
        self.chat.completions = type(self).completions

    async def __aenter__(self) -> StubAsyncOpenAI:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


@pytest.fixture
def stub_openai(monkeypatch):
    import strands.models.openai as strands_openai

    StubAsyncOpenAI.constructed = []
    StubAsyncOpenAI.completions = _Completions()
    monkeypatch.setattr(strands_openai.openai, "AsyncOpenAI", StubAsyncOpenAI)
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", "sf-key")
    monkeypatch.setenv(aws_clients.GLOBAL_BEDROCK_BEARER_ENV, "bearer")
    aws_clients.reset_cache()
    yield StubAsyncOpenAI
    aws_clients.reset_cache()


def _requests() -> list[dict[str, Any]]:
    return StubAsyncOpenAI.completions.requests


# ─── the hub's judge model ─────────────────────────────────────────────────────
def test_the_hub_openai_judge_is_a_streaming_openai_model_with_the_judge_bounds(stub_openai):
    model = aws_clients.build_chat_model(SF, region="us-west-2", max_tokens=2048)
    assert isinstance(model, OpenAIModel)
    assert model.get_config()["stream"] is True
    # the client bounds (judge_client) travel as client arguments, built per request
    assert model.client_args["timeout"] == judge_client.JUDGE_OPENAI_TIMEOUT_S == 300
    assert model.client_args["max_retries"] == judge_client.JUDGE_CLIENT_ATTEMPTS - 1 == 1


def test_a_structured_output_judge_call_streams_and_accumulates_the_tool_call(stub_openai):
    """What every strands-evals evaluator does (``evaluator_agent(prompt,
    structured_output_model=…)``): one streamed request, the rating assembled from the
    tool-call argument deltas, usage from the ``include_usage`` frame."""
    model = aws_clients.build_chat_model(SF, region="us-west-2", max_tokens=2048)
    result = Agent(model=model, system_prompt="judge", callback_handler=None)(
        "judge this transcript", structured_output_model=Rating)

    assert result.structured_output == Rating(**RATING)
    assert result.stop_reason == "tool_use"
    assert result.metrics.accumulated_usage == {"inputTokens": 7, "outputTokens": 3,
                                                "totalTokens": 10}
    (request,) = _requests()
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}
    assert request["model"] == "zai-org/GLM-5.3" and request["max_tokens"] == 2048
    assert [t["function"]["name"] for t in request["tools"]] == ["Rating"]
    # the client was built from the judge's client_args: endpoint, key and bounds
    (client,) = stub_openai.constructed
    assert client["base_url"] == "https://api.siliconflow.cn/v1" and client["api_key"] == "sf-key"
    assert (client["timeout"], client["max_retries"]) == (300, 1)


def test_the_reflection_text_call_streams_and_joins_the_deltas(stub_openai):
    """``jobs_recommend.model_converse`` (recommend / GEPA-lite reflections): a text-mode
    Agent turn — streamed, the answer is the joined text deltas, reasoning excluded."""
    model = aws_clients.build_chat_model(SF, region="us-west-2", max_tokens=1024)
    converse = jobs_recommend.model_converse(model)
    text, usage = converse(SF, "you reflect", [{"role": "user", "text": "improve"}], 512)

    assert text == "".join(TEXT_PARTS)
    assert usage == {"input_tokens": 7, "output_tokens": 3, "stop_reason": "end_turn"}
    (request,) = _requests()
    assert request["stream"] is True and request["stream_options"] == {"include_usage": True}
    assert request["max_tokens"] == 512 and not request.get("tools")


# ─── streamed == single-body ───────────────────────────────────────────────────
def _model(stream: bool) -> OpenAIModel:
    return OpenAIModel(client=StubAsyncOpenAI(), model_id="m", params={"max_tokens": 256},
                       stream=stream)


def test_a_streamed_judgement_equals_the_single_body_one(stub_openai):
    streamed = Agent(model=_model(True), system_prompt="judge", callback_handler=None)(
        "judge", structured_output_model=Rating)
    single = Agent(model=_model(False), system_prompt="judge", callback_handler=None)(
        "judge", structured_output_model=Rating)
    assert streamed.structured_output == single.structured_output == Rating(**RATING)
    assert streamed.stop_reason == single.stop_reason == "tool_use"
    assert streamed.metrics.accumulated_usage == single.metrics.accumulated_usage
    first, second = _requests()
    assert (first["stream"], second["stream"]) == (True, False)
    assert "stream_options" in first and "stream_options" not in second


def test_a_streamed_reflection_equals_the_single_body_one(stub_openai):
    messages = [{"role": "user", "text": "x"}]
    streamed = jobs_recommend.model_converse(_model(True))("m", "s", messages, 256)
    single = jobs_recommend.model_converse(_model(False))("m", "s", messages, 256)
    assert streamed == single == (
        "".join(TEXT_PARTS), {"input_tokens": 7, "output_tokens": 3, "stop_reason": "end_turn"})


def test_only_the_catalog_streaming_flag_turns_a_judge_into_a_single_body_request(stub_openai):
    """A row published with the ``nostream`` trait (an endpoint that rejects ``stream``)
    keeps the single-body request — the flag is the one switch, and it is per row."""
    from app.schemas import model_catalog
    from tests import model_fixtures

    model_catalog.reset()
    model_fixtures.publish_siliconflow("zai-org/GLM-5.3:tools+nostream")
    model = aws_clients.build_chat_model(SF, region="us-west-2")
    assert isinstance(model, OpenAIModel) and model.get_config()["stream"] is False
    Agent(model=model, system_prompt="judge", callback_handler=None)(
        "q", structured_output_model=Rating)
    (request,) = _requests()
    assert request["stream"] is False and "stream_options" not in request
