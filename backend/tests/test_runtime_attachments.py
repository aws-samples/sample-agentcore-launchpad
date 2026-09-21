"""Execute native-input contracts without AWS, SDK subprocesses, or model calls."""

import ast
import asyncio
import base64
import copy
import mimetypes
from pathlib import Path
from types import SimpleNamespace

import pytest
import strands

from app.schemas.agent import AgentSpec
from app.services.harness_convert import (
    ConversionError,
    graft_mantle_attachment_model,
    graft_runtime_attachments,
)
from app.templates.attachment_support import has_attachment_contract, render_attachment_source
from app.templates.studio_agent import adapt_studio_code

from .test_claude_sdk_template import _import_rendered as import_claude
from .test_strands_template import SPEC
from .test_strands_template import _import_rendered as import_strands

IMAGE = b"\x89PNG\r\n\x1a\n\x00\xff\x80binary image"
PDF = b"%PDF-1.7\n\x00\xff\x80binary document"
FILES = [
    {
        "name": "attachment-1.png",
        "media_type": "image/png",
        "data": base64.b64encode(IMAGE).decode(),
    },
    {
        "name": "attachment-2.pdf",
        "media_type": "application/pdf",
        "data": base64.b64encode(PDF).decode(),
    },
]
ACK = {"event": "attachments", "contract": "v1"}


def helper_namespace():
    namespace = {}
    exec(compile(render_attachment_source(), "attachments.py", "exec"), namespace)
    return namespace


async def collect(events):
    return [event async for event in events]


def assert_binary_content(content):
    assert content[-2] == {"image": {"format": "png", "source": {"bytes": IMAGE}}}
    assert content[-1] == {
        "document": {"format": "pdf", "name": "attachment-2", "source": {"bytes": PDF}}
    }


@pytest.mark.parametrize("invalid", ["not base64!", "YWJj\n", "é", "", None, 123])
@pytest.mark.parametrize("adapter", ["_launchpad_strands_input", "_launchpad_claude_input"])
def test_invalid_base64_is_rejected(adapter, invalid):
    files = [{**FILES[0], "data": invalid}]
    with pytest.raises(ValueError, match="attachment"):
        helper_namespace()[adapter]("read", files)


@pytest.mark.parametrize("media_type", ["image/png", "image/jpeg", "image/gif", "image/webp"])
def test_image_format_and_bytes_are_preserved(media_type):
    content = helper_namespace()["_launchpad_strands_input"](
        "read", [{**FILES[0], "media_type": media_type}]
    )
    assert content == [
        {"text": "read"},
        {"image": {"format": media_type.split("/")[1], "source": {"bytes": IMAGE}}},
    ]


def test_native_helper_rejects_text_and_unknown_binary():
    for media_type in ("text/plain", "application/zip"):
        with pytest.raises(ValueError, match="unsupported"):
            helper_namespace()["_launchpad_strands_input"](
                "read", [{**FILES[0], "media_type": media_type}]
            )


@pytest.mark.parametrize("prompt", ["read the files", ""])
def test_strands_delivers_bytes_after_ack_and_keeps_memory_query_text(
    tmp_path, monkeypatch, prompt
):
    module = import_strands(SPEC, tmp_path, monkeypatch, "attachment_strands")
    seen = []

    class Agent:
        async def stream_async(self, content):
            seen.append(content)
            yield {"data": "answer"}
            yield {"result": "answer"}

    memory_queries = []
    monkeypatch.setattr(module, "memory_session_manager", lambda *args: None)
    monkeypatch.setattr(module, "build_agent", lambda *args, **kwargs: Agent())
    monkeypatch.setattr(module, "recall_context", lambda actor, text: memory_queries.append(text))

    async def run():
        events = module.invoke({"prompt": prompt, "attachments": FILES})
        assert await anext(events) == ACK
        # The heartbeat producer may already have begun model work, but no model
        # event is exposed until the acknowledgement has been consumed.
        rest = await collect(events)
        assert rest[0] == {"event": "delta", "text": "answer"}
        assert rest[-1]["event"] == "complete"

    asyncio.run(run())
    assert len(seen) == 1
    assert_binary_content(seen[0])
    assert memory_queries == [prompt]
    assert module.LAUNCHPAD_ATTACHMENT_CONTRACT == "v1"


def test_strands_invalid_attachment_has_no_ack_or_model_call(tmp_path, monkeypatch):
    module = import_strands(SPEC, tmp_path, monkeypatch, "invalid_attachment_strands")
    monkeypatch.setattr(module, "memory_session_manager", lambda *args: None)
    monkeypatch.setattr(module, "build_agent", lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(module, "recall_context", lambda *args: "")
    with pytest.raises(ValueError, match="base64"):
        asyncio.run(
            collect(
                module.invoke(
                    {
                        "prompt": "read",
                        "attachments": [{**FILES[0], "data": "invalid!"}],
                    }
                )
            )
        )


def test_claude_async_messages_ack_and_text_memory(tmp_path, monkeypatch):
    module = import_claude(
        AgentSpec(name="claude-attachments", method="container", system_prompt="help"),
        tmp_path,
        monkeypatch,
        "attachment_claude",
    )
    queried = []
    saved = []
    model_input = []
    memory = SimpleNamespace(
        context_for=lambda text: queried.append(text) or "memory",
        save_turn=lambda prompt, result: saved.append((prompt, result)),
    )
    monkeypatch.setattr(module, "_create_memory", lambda *args: memory)

    async def query(*, prompt, options):
        model_input.extend(await collect(prompt))
        hook = options.hooks["UserPromptSubmit"][0].hooks[0]
        await hook({"prompt": "SDK multimodal prompt representation"}, None, {})
        yield module.AssistantMessage(content=[module.TextBlock(text="answer")], model="claude")

    monkeypatch.setattr(module, "query", query)
    events = asyncio.run(collect(module.invoke({"prompt": "read files", "attachments": FILES})))
    assert events[0] == ACK
    assert events[1] == {"event": "delta", "text": "answer"}
    content = model_input[0]["message"]["content"]
    assert content[0] == {"type": "text", "text": "read files"}
    assert [block["type"] for block in content[1:]] == ["image", "document"]
    assert [base64.b64decode(block["source"]["data"]) for block in content[1:]] == [IMAGE, PDF]
    assert queried == ["read files"]
    assert saved == [("read files", "answer")]


@pytest.mark.parametrize("attachments", [None, []])
def test_plain_text_keeps_both_sdk_inputs_as_strings(attachments):
    namespace = helper_namespace()
    for adapter in ("_launchpad_strands_input", "_launchpad_claude_input"):
        assert namespace[adapter]("ordinary text", attachments) == "ordinary text"


def test_studio_new_code_accepts_files_and_old_code_remains_text_only():
    code = """async def main(user_input, messages):
    received.append(user_input)
    return "answer"
"""
    for marker in ("", "LAUNCHPAD_ATTACHMENT_CONTRACT = 'v1'\n"):
        source = adapt_studio_code(marker + code)
        namespace = {"received": []}
        exec(compile(source, "studio.py", "exec"), namespace)
        result = asyncio.run(namespace["invoke"]({"prompt": "read", "attachments": FILES}))
        if marker:
            assert result == {"attachment_contract": "v1", "result": "answer"}
            assert_binary_content(namespace["received"][0])
            assert has_attachment_contract(source)
        else:
            assert "error" in result
            assert namespace["received"] == []
            assert not has_attachment_contract(source)
        assert asyncio.run(namespace["invoke"]({"prompt": "text"})) == {"result": "answer"}
        assert namespace["received"][-1] == "text"
        assert adapt_studio_code(source) == source


@pytest.mark.parametrize(
    "code",
    [
        "# LAUNCHPAD_ATTACHMENT_CONTRACT = 'v1'",
        "description = \"LAUNCHPAD_ATTACHMENT_CONTRACT = 'v1'\"",
        "def f():\n    LAUNCHPAD_ATTACHMENT_CONTRACT = 'v1'",
        "LAUNCHPAD_ATTACHMENT_CONTRACT = 'v2'",
    ],
)
def test_contract_marker_requires_top_level_v1_literal(code):
    assert not has_attachment_contract(code)


@pytest.mark.parametrize(
    "fixture",
    [
        "harness_export_main.py",
        "harness_export_nomemory_main.py",
        "harness_export_skills_nomemory_main.py",
    ],
)
def test_conversion_executes_bytes_ack_order_and_text_compatibility(fixture):
    source = (Path(__file__).parent / "fixtures" / fixture).read_text()
    grafted = graft_runtime_attachments(source)
    assert graft_runtime_attachments(grafted) == grafted
    assert has_attachment_contract(grafted)
    compile(grafted, "converted.py", "exec")
    # Execute the real exported entrypoint + extraction logic with runtime/model
    # setup stubbed; its watchdog, filtering, and event yields run unchanged.
    tree = ast.parse(grafted)
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (node.name.startswith("_launchpad_") or node.name in ("invoke", "_extract_prompt"))
    ]
    for node in selected:
        node.decorator_list = []
    seen = []

    class Agent:
        async def stream_async(self, content):
            seen.append(content)
            yield {"event": {"contentBlockDelta": {"delta": {"text": "answer"}}}}

    namespace = {
        "asyncio": asyncio,
        "log": SimpleNamespace(info=lambda *a: None),
        "get_or_create_agent": lambda *args: Agent(),
        "resolve_s3_skills": lambda *args: [],
        "AgentSkills": lambda **kwargs: None,
    }
    exec(compile(ast.Module(body=selected, type_ignores=[]), "converted.py", "exec"), namespace)

    async def run():
        events = namespace["invoke"]({"prompt": "read", "attachments": FILES}, SimpleNamespace())
        assert await anext(events) == ACK
        assert seen == []
        assert (await collect(events))[0]["event"]["contentBlockDelta"]
        assert_binary_content(seen[0][0]["content"])
        plain = await collect(namespace["invoke"]({"prompt": "text"}, SimpleNamespace()))
        assert all(event != ACK for event in plain)
        assert seen[-1] == "text"

    asyncio.run(run())


def test_conversion_appends_to_messages_without_mutating_history():
    messages = [{"role": "user", "content": [{"text": "read files"}]}]
    original = copy.deepcopy(messages)
    result = helper_namespace()["_launchpad_attachment_messages"](messages, FILES)
    assert_binary_content(result[0]["content"])
    assert result[0]["content"][0] == {"text": "read files"}
    assert messages == original


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace("prompt = _extract_prompt(payload)", "prompt = 'ignored'"),
        lambda text: text.replace("agent.stream_async(", "agent.other_stream("),
        lambda text: text.replace(
            "prompt = _extract_prompt(payload)",
            "prompt = _extract_prompt(payload)\n    prompt = ''",
        ),
        lambda text: text.replace("@app.entrypoint", "@other.entrypoint"),
        lambda text: text.replace(
            "    prompt = _extract_prompt(payload)",
            "    yield {'data': 'early'}\n    prompt = _extract_prompt(payload)",
        ),
        lambda text: text.replace(
            "prompt = _extract_prompt(payload)", "prompt = _extract_prompt(payload); other = 1"
        ),
    ],
)
def test_conversion_rejects_drifted_anchors(mutation):
    source = (Path(__file__).parent / "fixtures" / "harness_export_main.py").read_text()
    with pytest.raises(ConversionError, match="attachment graft"):
        graft_runtime_attachments(mutation(source))


def test_conversion_rejects_modified_existing_graft():
    source = (Path(__file__).parent / "fixtures" / "harness_export_main.py").read_text()
    grafted = graft_runtime_attachments(source)
    for damaged in (
        grafted.replace('yield {"event": "attachments", "contract": "v1"}', "pass"),
        grafted.replace("validate=True", "validate=False"),
    ):
        with pytest.raises(ConversionError, match="modified"):
            graft_runtime_attachments(damaged)


def stock_responses_formatter():
    """Run the installed SDK's pure formatter without requiring its OpenAI extra."""
    source = (Path(strands.__file__).parent / "models" / "openai_responses.py").read_text()
    tree = ast.parse(source)
    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_encode_media_to_data_url"
        or isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id
            in ("_MAX_MEDIA_SIZE_BYTES", "_MAX_MEDIA_SIZE_LABEL", "_DEFAULT_MIME_TYPE")
            for target in node.targets
        )
    ]
    model = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "OpenAIResponsesModel"
    )
    model.bases = []
    model.body = [
        node
        for node in model.body
        if isinstance(node, ast.FunctionDef) and node.name == "_format_request_message_content"
    ]
    namespace = {"base64": base64, "mimetypes": mimetypes}
    code = "from __future__ import annotations\n" + ast.unparse(
        ast.Module(body=[*nodes, model], type_ignores=[])
    )
    exec(compile(code, "stock_responses_formatter.py", "exec"), namespace)
    return namespace["OpenAIResponsesModel"]


def test_mantle_formatter_preserves_pdf_image_and_text():
    base = stock_responses_formatter()
    model = helper_namespace()["_launchpad_mantle_model_class"](base)
    content = helper_namespace()["_launchpad_strands_input"]("read", FILES)
    pdf = model._format_request_message_content(content[-1])
    assert pdf == {
        "type": "input_file",
        "filename": "attachment-2.pdf",
        "file_data": "data:application/pdf;base64," + FILES[1]["data"],
    }
    for block, role in ((content[0], "user"), (content[0], "assistant"), (content[1], "user")):
        assert model._format_request_message_content(block, role=role) == (
            base._format_request_message_content(block, role=role)
        )


@pytest.mark.parametrize("model_class", ["OpenAIResponsesModel", "MantleCompatResponsesModel"])
def test_converted_mantle_model_graft_preserves_constructor_and_is_idempotent(model_class):
    source = f"""def load_model():
    return {model_class}(model_id="original", params={{"store": False}})
"""
    grafted = graft_mantle_attachment_model(source)
    assert graft_mantle_attachment_model(grafted) == grafted
    captured = {}

    class Base(stock_responses_formatter()):
        def __init__(self, **kwargs):
            captured.update(kwargs)

    namespace = {model_class: Base}
    exec(compile(grafted, "model_load.py", "exec"), namespace)
    model = namespace["load_model"]()
    block = helper_namespace()["_launchpad_strands_input"]("read", FILES)[-1]
    assert model._format_request_message_content(block)["file_data"].endswith(FILES[1]["data"])
    assert captured == {"model_id": "original", "params": {"store": False}}


def test_converted_mantle_model_graft_rejects_unknown_model():
    with pytest.raises(ConversionError, match="constructor"):
        graft_mantle_attachment_model("def load_model():\n    return UnknownModel()\n")
