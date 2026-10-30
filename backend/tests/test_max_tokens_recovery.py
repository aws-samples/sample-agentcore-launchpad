"""N-SCN-bug4: a `max_tokens` stop is recovered by the generated runtimes, never raised.

Strands raises ``MaxTokensReachedException`` (unwrapped) out of ``stream_async`` when one
completion stops on ``max_tokens`` and leaves the partial assistant message in the agent's
history. Both generated templates (Harness ``harness_runtime/main.py.tmpl`` and the shared
Strands ``strands_agent/main.py.tmpl`` used by zip_runtime / Studio) continue the turn a
bounded number of times (``LAUNCHPAD_MAX_TOKENS_CONTINUATIONS``, default 1) and otherwise
end the turn with a visible notice plus a ``truncated: true`` marker. The hub forwards the
marker additively. Everything here is hermetic: fake agents, no AWS.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
import types
from pathlib import Path

import pytest
import strands
from strands.types.exceptions import MaxTokensReachedException

from app.deployer import environment as env_mod
from app.deployer import harness_local
from app.schemas.agent import AgentSpec
from app.services.agentcore import runtime as rt
from app.templates.strands_agent import render_main_py

from .conftest import ws_ctx

STRANDS_MESSAGE = (
    "Model stopped generating due to maximum token limit. The partial message has been "
    "added to the conversation history. You can continue by calling the agent again."
)
NOTICE = "[response truncated at the model's token limit]"
CONTINUATION = (
    "Your previous answer was cut off at the token limit. Finish it concisely - final answer only."
)
PROMPT = "Use the code interpreter to compute 13 x 8750 + 12 x 1250"


def _drain(agen):
    async def collect():
        return [event async for event in agen]

    return asyncio.run(collect())


def _usage(output_tokens: int) -> dict:
    return {"event": {"metadata": {"usage": {"inputTokens": 900, "outputTokens": output_tokens}}}}


# --- shared Strands template (zip_runtime / Studio) -------------------------------------

SPEC = AgentSpec(
    name="maxtok-test-agent", method="zip_runtime", system_prompt="Be brief."
)


@pytest.fixture
def strands_module(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    target = tmp_path / "rendered_main.py"
    target.write_text(render_main_py(SPEC), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("rendered_maxtok_main", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv("LAUNCHPAD_MAX_TOKENS_CONTINUATIONS", raising=False)
    return module


class _StrandsFakeAgent:
    """``stream_async`` plays one scripted turn per call; a script ending in an exception
    instance raises it after its events (the partial message is 'already in history')."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.prompts: list = []

    async def stream_async(self, prompt):
        self.prompts.append(prompt)
        turn = self.turns.pop(0)
        for item in turn:
            if isinstance(item, BaseException):
                raise item
            yield item


def test_strands_continuation_finishes_the_cut_off_turn(strands_module, caplog):
    agent = _StrandsFakeAgent(
        [{"data": "The total is "}, _usage(16384), MaxTokensReachedException(STRANDS_MESSAGE)],
        [{"data": "128750."}, {"result": "128750."}],
    )
    with caplog.at_level(logging.WARNING, logger="launchpad.runtime"):
        events = _drain(strands_module.agent_events(agent, PROMPT))

    assert events == [
        {"event": "delta", "text": "The total is "},
        {"event": "truncated", "truncated": False, "stop_reason": "max_tokens",
         "continuations": 1},
        {"event": "delta", "text": "\n"},
        {"event": "delta", "text": "128750."},
        {"event": "complete", "result": "The total is \n128750."},
    ]
    assert agent.prompts == [PROMPT, CONTINUATION]
    warn = [r for r in caplog.records if "max_tokens stop" in r.getMessage()]
    assert len(warn) == 1 and warn[0].levelno == logging.WARNING
    line = warn[0].getMessage()
    assert "stop_reason=max_tokens" in line and "output_tokens=16384" in line
    assert "continuation=0/1" in line
    assert "8750" not in line and "code interpreter" not in line  # never the prompt text


def test_strands_continuation_also_truncated_ends_with_notice_and_marker(strands_module):
    agent = _StrandsFakeAgent(
        [{"data": "part one"}, MaxTokensReachedException(STRANDS_MESSAGE)],
        [{"data": "part two"}, MaxTokensReachedException(STRANDS_MESSAGE)],
    )
    events = _drain(strands_module.agent_events(agent, PROMPT))

    assert events == [
        {"event": "delta", "text": "part one"},
        {"event": "truncated", "truncated": False, "stop_reason": "max_tokens",
         "continuations": 1},
        {"event": "delta", "text": "\n"},
        {"event": "delta", "text": "part two"},
        {"event": "delta", "text": "\n\n" + NOTICE},
        {"event": "truncated", "truncated": True, "stop_reason": "max_tokens",
         "continuations": 1},
        {"event": "complete", "result": "part one\npart two\n\n" + NOTICE, "truncated": True},
    ]
    assert len(agent.prompts) == 2  # bounded: default budget is one continuation


def test_strands_textless_continuation_that_ends_normally_is_not_labelled_truncated(
    strands_module,
):
    # r7-e: the continuation stopped on end_turn without text - an empty answer, not a
    # truncated one: no notice, no `truncated: true`, a plain `complete`
    agent = _StrandsFakeAgent(
        [_usage(16384), MaxTokensReachedException(STRANDS_MESSAGE)],
        [{"result": ""}],
    )
    events = _drain(strands_module.agent_events(agent, PROMPT))

    assert events == [
        {"event": "truncated", "truncated": False, "stop_reason": "max_tokens",
         "continuations": 1},
        {"event": "complete", "result": ""},
    ]
    assert agent.prompts == [PROMPT, CONTINUATION]
    assert not any(NOTICE in str(e.get("text", "")) for e in events)


def test_strands_zero_continuations_emits_the_notice_directly(strands_module, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_MAX_TOKENS_CONTINUATIONS", "0")
    # the N-SCN-bug4 shape: the whole budget went to reasoning, no text at all
    agent = _StrandsFakeAgent([_usage(16384), MaxTokensReachedException(STRANDS_MESSAGE)])
    events = _drain(strands_module.agent_events(agent, PROMPT))

    assert events == [
        {"event": "delta", "text": NOTICE},
        {"event": "truncated", "truncated": True, "stop_reason": "max_tokens",
         "continuations": 0},
        {"event": "complete", "result": NOTICE, "truncated": True},
    ]
    assert agent.prompts == [PROMPT]


def test_strands_budget_env_is_defensive(strands_module, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_MAX_TOKENS_CONTINUATIONS", "garbage")
    assert strands_module._launchpad_max_tokens_continuations() == 1
    monkeypatch.setenv("LAUNCHPAD_MAX_TOKENS_CONTINUATIONS", "-3")
    assert strands_module._launchpad_max_tokens_continuations() == 0
    monkeypatch.setenv("LAUNCHPAD_MAX_TOKENS_CONTINUATIONS", "2")
    assert strands_module._launchpad_max_tokens_continuations() == 2


def test_strands_other_exceptions_still_surface(strands_module):
    agent = _StrandsFakeAgent([{"data": "x"}, ValueError("boom")])
    with pytest.raises(ValueError, match="boom"):
        _drain(strands_module.agent_events(agent, PROMPT))
    assert len(agent.prompts) == 1


def test_strands_recovery_matches_the_sdk_exception_by_name(strands_module):
    """The SDK is a moving preview target: the class is recognised by name, so a relocated
    import path (or a subclass) still recovers while a look-alike message does not."""

    class MaxTokensReachedException(Exception):  # noqa: N818 — another module's class
        pass

    assert strands_module._launchpad_is_max_tokens(MaxTokensReachedException("x"))
    assert not strands_module._launchpad_is_max_tokens(RuntimeError(STRANDS_MESSAGE))


def test_strands_normal_turn_is_unchanged(strands_module):
    agent = _StrandsFakeAgent([{"data": "Hello"}, {"result": "Hello"}])
    assert _drain(strands_module.agent_events(agent, "hi")) == [
        {"event": "delta", "text": "Hello"}, {"event": "complete", "result": "Hello"},
    ]


def test_strands_invoke_streams_partial_marker_continuation_in_order(
    strands_module, monkeypatch,
):
    """Through the entrypoint (heartbeat pump included): the partial text the console
    already saw stays, the marker separates it, the continuation follows once — no
    duplicate text and no error frame."""
    agent = _StrandsFakeAgent(
        [{"data": "The total is "}, MaxTokensReachedException(STRANDS_MESSAGE)],
        [{"data": "128750."}, {"result": "128750."}],
    )
    monkeypatch.setattr(strands_module, "memory_session_manager", lambda a, s: None)
    monkeypatch.setattr(
        strands_module, "build_agent",
        lambda actor_id, session_id, extra_tools=None, session_manager=None: agent,
    )
    out = _drain(strands_module.invoke({"prompt": PROMPT}))
    kinds = [e["event"] for e in out]
    assert kinds == ["delta", "truncated", "delta", "delta", "complete"]
    assert "".join(e["text"] for e in out if e["event"] == "delta") == "The total is \n128750."
    assert out[-1] == {"event": "complete", "result": "The total is \n128750."}


# --- self-hosted Harness template (方式B) -------------------------------------------------

RES = {
    "artifacts_bucket": "launchpad-artifacts-test",
    "memory_id": "mem-1",
    "execution_role_arn": "arn:aws-cn:iam::111122223333:role/launchpad-agent-execution-role",
}


def _import_real_harness(tmp_path: Path, monkeypatch, request):
    """Render the REAL vendored template for a plain Harness spec and import the bundle as
    the runtime would (the Strands Agent stubbed, bundle packages evicted at teardown)."""
    monkeypatch.setattr(harness_local, "TEMPLATE_DIR", harness_local.TEMPLATES / "harness_runtime")
    spec = AgentSpec(name="hr-maxtok", method="harness", system_prompt="You answer HR questions.")
    bundle = harness_local.render_harness_bundle(spec, ws_ctx(RES, region="cn-northwest-1"))
    root = tmp_path / "bundle"
    for rel, text in bundle.files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    monkeypatch.delenv("LAUNCHPAD_MEMORY_ID", raising=False)
    monkeypatch.delenv("LAUNCHPAD_MAX_TOKENS_CONTINUATIONS", raising=False)
    before = set(sys.modules)
    packages = {"model", "memory", "skills", "hooks", "mcp_client", "launchpad_memory"}

    def _evict():
        for name in set(sys.modules) - before:
            if name.split(".")[0] in packages:
                sys.modules.pop(name, None)

    request.addfinalizer(_evict)
    module_spec = importlib.util.spec_from_file_location(
        "rendered_maxtok_harness", root / "main.py"
    )
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(module, "resolve_s3_skills", lambda sources, _creds: [])
    return module


class _HarnessFakeAgent(_StrandsFakeAgent):
    def cancel(self):  # the invoke watchdog's hook
        pass


def _delta(text: str) -> dict:
    return {"event": {"contentBlockDelta": {"delta": {"text": text}, "contentBlockIndex": 0}}}


TOOL_START = {"event": {"contentBlockStart": {"start": {"toolUse": {
    "toolUseId": "t1", "name": "code_interpreter"}}, "contentBlockIndex": 0}}}


def _wire(module, monkeypatch, agent):
    monkeypatch.setattr(
        module, "get_or_create_agent", lambda session_id, user_id, skill_plugins=None: agent,
    )
    return types.SimpleNamespace(session_id="sess-1", user_id="hr-maxtok__river")


def test_harness_continuation_streams_after_the_marker(tmp_path, monkeypatch, request, caplog):
    module = _import_real_harness(tmp_path, monkeypatch, request)
    agent = _HarnessFakeAgent(
        [TOOL_START, _delta("The total is "), _usage(16384),
         MaxTokensReachedException(STRANDS_MESSAGE)],
        [_delta("128750."), {"event": {"messageStop": {"stopReason": "end_turn"}}}],
    )
    ctx = _wire(module, monkeypatch, agent)
    with caplog.at_level(logging.WARNING):
        out = _drain(module.invoke({"prompt": PROMPT}, ctx))

    assert out == [
        TOOL_START,
        _delta("The total is "),
        _usage(16384),
        {"event": "truncated", "truncated": False, "stop_reason": "max_tokens",
         "continuations": 1},
        _delta("\n"),
        _delta("128750."),
        {"event": {"messageStop": {"stopReason": "end_turn"}}},
    ]
    assert agent.prompts == [PROMPT, CONTINUATION]
    warn = [r for r in caplog.records if "max_tokens stop" in r.getMessage()]
    assert len(warn) == 1 and warn[0].levelno == logging.WARNING
    assert "output_tokens=16384" in warn[0].getMessage()
    assert "continuation=1/1" in warn[0].getMessage()
    assert "8750" not in warn[0].getMessage()


def test_harness_continuation_also_truncated_ends_with_notice(tmp_path, monkeypatch, request):
    module = _import_real_harness(tmp_path, monkeypatch, request)
    agent = _HarnessFakeAgent(
        [_delta("part one"), MaxTokensReachedException(STRANDS_MESSAGE)],
        [_delta("part two"), MaxTokensReachedException(STRANDS_MESSAGE)],
    )
    ctx = _wire(module, monkeypatch, agent)
    out = _drain(module.invoke({"prompt": PROMPT}, ctx))

    assert out == [
        _delta("part one"),
        {"event": "truncated", "truncated": False, "stop_reason": "max_tokens",
         "continuations": 1},
        _delta("\n"),
        _delta("part two"),
        _delta("\n\n" + NOTICE),
        {"event": "truncated", "truncated": True, "stop_reason": "max_tokens",
         "continuations": 1},
    ]
    assert len(agent.prompts) == 2


def test_harness_textless_continuation_that_ends_normally_is_not_labelled_truncated(
    tmp_path, monkeypatch, request,
):
    # r7-e: same as the Strands case - end_turn with no text ends the turn normally
    module = _import_real_harness(tmp_path, monkeypatch, request)
    agent = _HarnessFakeAgent(
        [TOOL_START, _usage(16384), MaxTokensReachedException(STRANDS_MESSAGE)],
        [{"event": {"messageStop": {"stopReason": "end_turn"}}}],
    )
    ctx = _wire(module, monkeypatch, agent)
    out = _drain(module.invoke({"prompt": PROMPT}, ctx))

    assert out == [
        TOOL_START,
        _usage(16384),
        {"event": "truncated", "truncated": False, "stop_reason": "max_tokens",
         "continuations": 1},
        {"event": {"messageStop": {"stopReason": "end_turn"}}},
    ]
    assert agent.prompts == [PROMPT, CONTINUATION]


def test_harness_zero_continuations_emits_the_notice_directly(tmp_path, monkeypatch, request):
    module = _import_real_harness(tmp_path, monkeypatch, request)
    monkeypatch.setenv("LAUNCHPAD_MAX_TOKENS_CONTINUATIONS", "0")
    agent = _HarnessFakeAgent([TOOL_START, MaxTokensReachedException(STRANDS_MESSAGE)])
    ctx = _wire(module, monkeypatch, agent)
    out = _drain(module.invoke({"prompt": PROMPT}, ctx))

    assert out == [
        TOOL_START,
        _delta(NOTICE),  # no partial text → no leading blank line
        {"event": "truncated", "truncated": True, "stop_reason": "max_tokens",
         "continuations": 0},
    ]
    assert agent.prompts == [PROMPT]


def test_harness_other_exceptions_still_surface(tmp_path, monkeypatch, request):
    module = _import_real_harness(tmp_path, monkeypatch, request)
    agent = _HarnessFakeAgent([_delta("x"), RuntimeError("tool exploded")])
    ctx = _wire(module, monkeypatch, agent)
    with pytest.raises(RuntimeError, match="tool exploded"):
        _drain(module.invoke({"prompt": PROMPT}, ctx))
    # the export's own ExecutionLimitExceeded handling is untouched
    limits = module.EventLoopException(module.ExecutionLimitExceeded("limit hit"), {})
    agent = _HarnessFakeAgent([_delta("y"), limits])
    ctx = _wire(module, monkeypatch, agent)
    assert _drain(module.invoke({"prompt": PROMPT}, ctx)) == [
        _delta("y"), {"event": {"messageStop": {"stopReason": "limit hit"}}},
    ]


def test_harness_template_keeps_its_anchors():
    """The recovery lives in a module-level helper so A5's single
    `agent.stream_async(prompt)` inside `invoke` still holds (the gate's self-check)."""
    assert harness_local.check_template(harness_local.TEMPLATES / "harness_runtime") == []
    main = (harness_local.TEMPLATES / "harness_runtime" / "main.py.tmpl").read_text()
    assert main.count("# <launchpad-max-tokens:v1>") == 1


# --- hub: the marker is additive -----------------------------------------------------------


def test_hub_forwards_the_truncated_marker_instead_of_raw_json():
    marker = {"event": "truncated", "truncated": True, "stop_reason": "max_tokens",
              "continuations": 1}
    assert list(rt._runtime_payload_events(marker)) == [
        {"event": "truncated", "data": {"truncated": True, "stop_reason": "max_tokens",
                                        "continuations": 1}},
    ]
    assert list(rt._runtime_payload_events({"event": "truncated"})) == [
        {"event": "truncated", "data": {"truncated": False, "stop_reason": "max_tokens",
                                        "continuations": 0}},
    ]
    assert list(rt._runtime_payload_events(
        {"event": "complete", "result": "x " + NOTICE, "truncated": True}
    )) == [{"event": "complete", "data": {"text": "x " + NOTICE, "truncated": True}}]
    # a complete answer carries no marker key at all
    assert list(rt._runtime_payload_events({"event": "complete", "result": "ok"})) == [
        {"event": "complete", "data": {"text": "ok"}},
    ]


class _SseBody:
    def __init__(self, payloads: list) -> None:
        self._lines = [f"data: {json.dumps(p)}".encode() for p in payloads]

    def iter_lines(self, chunk_size=None):
        for line in self._lines:
            yield line
            yield b""


class _SseClient:
    def __init__(self, payloads: list) -> None:
        self.payloads = payloads

    def invoke_agent_runtime(self, **kwargs):
        return {"response": _SseBody(self.payloads), "contentType": "text/event-stream"}


ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/hr-abc123"


def test_hub_text_invoke_marks_a_truncated_turn_only():
    truncated = rt.invoke_runtime_text(_SseClient([
        {"event": "delta", "text": "part"},
        {"event": "delta", "text": "\n\n" + NOTICE},
        {"event": "truncated", "truncated": True, "stop_reason": "max_tokens",
         "continuations": 1},
        {"event": "complete", "result": "part\n\n" + NOTICE, "truncated": True},
    ]), ARN, "hi", session_id="s" * 40)
    assert truncated["text"] == "part\n\n" + NOTICE and truncated["truncated"] is True

    continued = rt.invoke_runtime_text(_SseClient([
        {"event": "delta", "text": "The total is "},
        {"event": "truncated", "truncated": False, "stop_reason": "max_tokens",
         "continuations": 1},
        {"event": "delta", "text": "\n128750."},
        {"event": "complete", "result": "The total is \n128750."},
    ]), ARN, "hi", session_id="s" * 40)
    assert continued["text"] == "The total is \n128750."
    assert "truncated" not in continued


# --- hub: the continuation budget reaches the runtime env -----------------------------------

PLAIN = AgentSpec(name="env-maxtok", method="zip_runtime", system_prompt="hi")


def test_runtime_env_carries_the_budget_only_when_the_operator_changed_it(monkeypatch):
    settings = env_mod.get_settings()
    assert env_mod.runtime_environment(PLAIN, {}).get("LAUNCHPAD_MAX_TOKENS_CONTINUATIONS") is None

    monkeypatch.setattr(
        env_mod, "get_settings",
        lambda: settings.model_copy(update={"max_tokens_continuations": 2}),
    )
    assert env_mod.runtime_environment(PLAIN, {})["LAUNCHPAD_MAX_TOKENS_CONTINUATIONS"] == "2"
    pinned = PLAIN.model_copy(update={"env": {"LAUNCHPAD_MAX_TOKENS_CONTINUATIONS": "0"}})
    assert env_mod.runtime_environment(pinned, {})["LAUNCHPAD_MAX_TOKENS_CONTINUATIONS"] == "0"
