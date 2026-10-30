"""N17-obs405 — a Studio agent replies with its final answer exactly once.

The wrapper returned the redirected stdout of the canvas ``main()``. An ``Agent`` built
without ``callback_handler`` gets Strands' ``PrintingCallbackHandler``, which streams the
model's text (reasoning, the pre-tool preamble), ``Tool #1: get_weather`` lines and the
answer to stdout; the canvas's own ``print(str(response))`` then printed the answer again
(live probe-studio-mt3: reasoning + tool trace + the answer twice). Now the reply is the
final text of the agent's result; stdout is kept for the logs only.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterable
from typing import Any

from strands.models.model import Model

from app.templates.studio_agent import adapt_studio_code

ANSWER = "**Shanghai weather:** 18°C, cloudy."


class ToolThenAnswer(Model):
    """Turn 1: preamble text + a get_weather toolUse; turn 2: the answer."""

    def __init__(self) -> None:
        self.calls = 0

    def update_config(self, **_kw: Any) -> None:
        pass

    def get_config(self) -> dict[str, Any]:
        return {"model_id": "fake"}

    async def structured_output(self, *a: Any, **kw: Any):  # pragma: no cover - unused
        raise NotImplementedError
        yield {}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs
                     ) -> AsyncIterable[dict[str, Any]]:
        self.calls += 1
        yield {"messageStart": {"role": "assistant"}}
        if self.calls == 1:
            yield {"contentBlockDelta": {"delta": {"text": "Let me check the weather for you."}}}
            yield {"contentBlockStop": {}}
            yield {"contentBlockStart": {"start": {"toolUse": {
                "toolUseId": "tu-1", "name": "get_weather"}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {
                "input": json.dumps({"city": "Shanghai"})}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": ANSWER}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                            "metrics": {"latencyMs": 1}}}


# the canvas shape of the probe: an Agent WITHOUT callback_handler (module scope), the sync
# execution path that prints and returns str(response)
CANVAS = '''
from strands import Agent, tool


@tool
def get_weather(city: str) -> str:
    """Weather of a city."""
    return "18C cloudy"


assistant = Agent(model=FAKE_MODEL, tools=[get_weather])


async def main(user_input_arg=None, messages_arg=None):
    response = assistant(user_input_arg)
    print(str(response))
    return str(response)
'''

STREAMING = '''
from strands import Agent, tool


@tool
def get_weather(city: str) -> str:
    """Weather of a city."""
    return "18C cloudy"


async def main(user_input_arg=None, messages_arg=None):
    agent = Agent(model=FAKE_MODEL, tools=[get_weather])
    async for event in agent.stream_async(user_input_arg):
        if "data" in event:
            print(event["data"], end="", flush=True)
'''


def _invoke(code: str, capsys) -> tuple[dict[str, Any], str]:
    namespace: dict[str, Any] = {"FAKE_MODEL": ToolThenAnswer()}
    exec(compile(adapt_studio_code(code), "studio.py", "exec"), namespace)
    capsys.readouterr()
    reply = asyncio.run(namespace["invoke"]({"prompt": "Weather in Shanghai?"}))
    return reply, capsys.readouterr().out


def test_a_studio_turn_with_a_tool_call_replies_with_the_answer_once(capsys):
    reply, _stdout = _invoke(CANVAS, capsys)
    assert reply == {"result": ANSWER}


def test_a_streaming_canvas_replies_with_the_final_answer_not_the_preamble(capsys):
    reply, _stdout = _invoke(STREAMING, capsys)
    assert reply == {"result": ANSWER}


def test_the_canvas_stdout_still_reaches_the_logs(capsys):
    """What main() prints still goes to the process stdout (the runtime's log stream)."""
    _reply, stdout = _invoke(STREAMING, capsys)
    assert "Let me check the weather for you." in stdout and ANSWER in stdout


def test_a_module_without_an_agent_keeps_the_stdout_reply():
    code = "async def main(user_input, messages):\n    print('plain', user_input)\n"
    namespace: dict[str, Any] = {}
    exec(compile(adapt_studio_code(code), "plain.py", "exec"), namespace)
    assert asyncio.run(namespace["invoke"]({"prompt": "x"})) == {"result": "plain x"}


class Plain(ToolThenAnswer):
    """Always answers ``INNER`` (an agent used as a tool)."""

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockDelta": {"delta": {"text": "INNER"}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


NESTED = '''
from strands import Agent, tool

helper = Agent(model=INNER_MODEL, callback_handler=None)


@tool
def get_weather(city: str) -> str:
    """Weather of a city (asks a helper agent)."""
    return str(helper(f"weather of {city}"))


assistant = Agent(model=FAKE_MODEL, tools=[get_weather])


async def main(user_input_arg=None, messages_arg=None):
    return str(assistant(user_input_arg))
'''


def test_an_agent_used_as_a_tool_is_never_the_reply():
    namespace: dict[str, Any] = {"FAKE_MODEL": ToolThenAnswer(), "INNER_MODEL": Plain()}
    exec(compile(adapt_studio_code(NESTED), "nested.py", "exec"), namespace)
    reply = asyncio.run(namespace["invoke"]({"prompt": "Weather in Shanghai?"}))
    assert reply == {"result": ANSWER}
