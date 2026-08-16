"""Claude SDK container template: render, build context, codebuild pipeline."""

import asyncio
import importlib.util
import json
import py_compile
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.schemas.agent import AgentSpec
from app.services.agentcore import codebuild as cb
from app.services.agentcore import runtime as rt
from app.templates.claude_sdk_agent import assemble_build_context, render_main_py

SPEC = AgentSpec(
    name="sdk-test-agent",
    method="container",
    system_prompt="You are a container test agent.",
    max_iterations=7,
)


async def collect_async(iterator):
    return [item async for item in iterator]


def test_render_replaces_placeholders():
    code = render_main_py(SPEC)
    assert "__LAUNCHPAD_" not in code
    assert "sdk-test-agent" in code
    assert "MAX_TURNS = 7" in code
    # Bedrock switch is baked into the Dockerfile env, never set in code
    assert 'os.environ["CLAUDE_CODE_USE_BEDROCK"]' not in code
    assert 'MEMORY_SHORT_TERM = "True" == "True"' in code
    assert 'MEMORY_LONG_TERM = "False" == "True"' in code
    assert "__LAUNCHPAD_" not in code


def test_render_memory_flags():
    spec = AgentSpec(
        **{
            **SPEC.model_dump(),
            "memory": {"short_term": False, "long_term": True},
        }
    )
    code = render_main_py(spec)
    assert 'MEMORY_SHORT_TERM = "False" == "True"' in code
    assert 'MEMORY_LONG_TERM = "True" == "True"' in code


def test_raw_memory_placeholders_fail_closed_for_stale_renderer():
    source = Path("app/templates/claude_sdk_agent/main.py.tmpl").read_text()
    assignments = "\n".join(
        line
        for line in source.splitlines()
        if line.startswith(("MEMORY_SHORT_TERM =", "MEMORY_LONG_TERM ="))
    )
    values: dict = {}
    exec(assignments, values)
    assert values["MEMORY_SHORT_TERM"] is False
    assert values["MEMORY_LONG_TERM"] is False


def test_render_parses_mcp_servers_from_env():
    spec = SPEC.model_copy(
        update={"env": {"LAUNCHPAD_MCP_SERVERS": '{"docs": {"command": "uvx", "args": ["x"]}}'}}
    )
    code = render_main_py(spec)
    assert "'docs'" in code and "'uvx'" in code
    # every configured server is allow-listed with Claude Code's mcp__ prefix
    assert "'mcp__docs'" in code


def test_render_default_allowed_tools():
    code = render_main_py(SPEC)
    assert "ALLOWED_TOOLS: list[str] = ['Task']" in code


def test_render_skills_enable_skill_tool():
    spec = SPEC.model_copy(update={"skills": ["s3://bkt/skills/web-analyzer/"]})
    code = render_main_py(spec)
    assert "ALLOWED_TOOLS: list[str] = ['Task', 'Skill']" in code


def test_render_merges_registry_mcp_over_free_text():
    """Registry-selected servers (spec.tools mcp refs) merge into MCP_SERVERS and
    win over a same-named free-text entry; both get mcp__ allow-list entries."""
    spec = AgentSpec(
        **{
            **SPEC.model_dump(),
            "tools": [
                {"type": "mcp", "name": "deepwiki", "config": {"url": "https://mcp.deepwiki.com/mcp"}},
                {"type": "mcp", "name": "docs", "config": {"url": "https://registry.example/mcp"}},
            ],
            "env": {"LAUNCHPAD_MCP_SERVERS": '{"docs": {"command": "uvx", "args": ["x"]}}'},
        }
    )
    code = render_main_py(spec)
    assert "'deepwiki': {'type': 'http', 'url': 'https://mcp.deepwiki.com/mcp'}" in code
    # registry wins
    assert "'docs': {'type': 'http', 'url': 'https://registry.example/mcp'}" in code
    assert "'mcp__deepwiki'" in code and "'mcp__docs'" in code
    assert "'uvx'" not in code  # the shadowed free-text entry is gone


def test_render_tolerates_bad_mcp_json():
    spec = SPEC.model_copy(update={"env": {"LAUNCHPAD_MCP_SERVERS": "{not json"}})
    code = render_main_py(spec)
    assert "MCP_SERVERS: dict[str, Any] = {}" in code
    assert "ALLOWED_TOOLS: list[str] = ['Task']" in code


def test_rendered_main_compiles(tmp_path: Path):
    target = tmp_path / "main.py"
    target.write_text(render_main_py(SPEC), encoding="utf-8")
    py_compile.compile(str(target), doraise=True)


KB_SPEC = AgentSpec(
    **{
        **SPEC.model_dump(),
        "knowledge_bases": [
            {"kb_id": "KB111", "name": "fund-docs", "description": "fund product PDFs"},
        ],
    }
)


def test_render_without_kbs_leaves_retrieval_inert():
    code = render_main_py(SPEC)
    assert "MOUNTED_KBS: list[dict[str, str]] = []" in code
    assert "KB_TOOL_DESCRIPTION = ''" in code
    assert "KB_DEEP_TOOL_DESCRIPTION = ''" in code
    # not allow-listed (the name still appears in the template's own comments)
    assert "ALLOWED_TOOLS: list[str] = ['Task']" in code
    assert "## Knowledge bases" not in code


def test_render_mounts_kb_mcp_server_and_prompt_section(tmp_path: Path):
    code = render_main_py(KB_SPEC)
    assert "__LAUNCHPAD_" not in code
    assert "'kb_id': 'KB111'" in code
    assert 'KB_MCP_SERVER = "launchpad_kb"' in code
    # server-level allow covers every tool the in-process server carries, so
    # adding kb_deep_search must NOT add an ALLOWED_TOOLS entry
    assert "ALLOWED_TOOLS: list[str] = ['Task', 'mcp__launchpad_kb']" in code
    assert "## Knowledge bases" in code
    assert "fund product PDFs" in code
    assert "KB_DEEP_TOOL_DESCRIPTION = 'Deep-search" in code
    target = tmp_path / "kb_main.py"
    target.write_text(code, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)


def test_rendered_main_uses_native_openinference_telemetry():
    code = render_main_py(SPEC)
    assert "from openinference.instrumentation import using_session" in code
    assert "with using_session(session_id):" in code
    assert "import tracing" not in code
    assert "tracing." not in code
    assert "ToolResultBlock" not in code
    assert "UserMessage" not in code


def test_container_requirements_pin_drifting_deps():
    """Open upper bounds on these dependencies can turn a green deploy into an agent
    that fails every invoke — the image is built fresh by CodeBuild each time."""
    requirements = Path("app/templates/claude_sdk_agent/requirements.txt").read_text()
    assert "aws-opentelemetry-distro==0.19.*" in requirements
    assert "claude-agent-sdk==0.2.*" in requirements
    assert "bedrock-agentcore==1.17.*" in requirements
    assert "openinference-instrumentation-claude-agent-sdk>=0.1.3,<0.2" in requirements


def test_assemble_build_context(tmp_path: Path):
    ctx = assemble_build_context(SPEC, tmp_path / "ctx")
    files = {str(p.relative_to(ctx)) for p in ctx.rglob("*") if p.is_file()}
    assert {"Dockerfile", "requirements.txt", "buildspec.yml", "main.py"} <= files
    assert "tracing.py" not in files
    # no baked-in subagents: the fact-checker sample was dropped (not SDK-native)
    assert not any(f.startswith(".claude/agents/") for f in files)
    dockerfile = (ctx / "Dockerfile").read_text()
    assert "linux/arm64" in dockerfile
    assert "python:3.12-slim-bookworm" in dockerfile
    assert "ca-certificates git" not in dockerfile
    assert "CLAUDE_CODE_USE_BEDROCK=1" in dockerfile
    assert "@anthropic-ai/claude-code" in dockerfile
    requirements = (ctx / "requirements.txt").read_text()
    assert "bedrock-agentcore==1.17.*" in requirements


def _import_rendered(spec, tmp_path: Path, monkeypatch, stem: str):
    """Import a rendered runtime with a side-effect-free OpenInference context."""
    @contextmanager
    def using_session(_session_id):
        yield

    openinference = ModuleType("openinference")
    instrumentation = ModuleType("openinference.instrumentation")
    instrumentation.using_session = using_session
    openinference.instrumentation = instrumentation
    monkeypatch.setitem(sys.modules, "openinference", openinference)
    monkeypatch.setitem(sys.modules, "openinference.instrumentation", instrumentation)
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    target = tmp_path / f"{stem}.py"
    target.write_text(render_main_py(spec), encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location(stem, target)
    module = importlib.util.module_from_spec(module_spec)
    monkeypatch.setitem(sys.modules, module_spec.name, module)
    module_spec.loader.exec_module(module)
    return module


@pytest.fixture
def rendered_memory_module(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_MEMORY_ID", "memory-123")
    spec = AgentSpec(
        **{
            **SPEC.model_dump(),
            "memory": {"short_term": True, "long_term": True},
        }
    )
    return _import_rendered(spec, tmp_path, monkeypatch, "rendered_claude_memory_main")


@pytest.fixture
def rendered_kb_module(tmp_path: Path, monkeypatch):
    return _import_rendered(KB_SPEC, tmp_path, monkeypatch, "rendered_claude_kb_main")


def test_kb_tools_are_bridged_as_one_in_process_mcp_server(rendered_kb_module):
    module = rendered_kb_module
    assert list(module._kb_mcp_servers()) == ["launchpad_kb"]
    assert [module.kb_search.name, module.kb_deep_search.name] == [
        "kb_search",
        "kb_deep_search",
    ]
    options = module.build_options()
    assert list(options.mcp_servers or {}) == ["launchpad_kb"]
    assert options.allowed_tools == ["Task", "mcp__launchpad_kb"]
    assert "## Knowledge bases" in module.SYSTEM_PROMPT
    assert "`kb_deep_search`" in module.SYSTEM_PROMPT


def test_kb_deep_search_tool_returns_answer_and_passages(rendered_kb_module, monkeypatch):
    module = rendered_kb_module
    calls: list[dict] = []

    class FakeRuntime:
        def agentic_retrieve_stream(self, **kwargs):
            calls.append(kwargs)
            return {"stream": iter([
                {"traceEvent": {"attributes": {"step": "Planning", "status": "SUCCEEDED"}}},
                {"responseEvent": {"text": "ignored delta"}},
                {"result": {
                    "generatedResponse": {"answer": "AUM is $10,706 MM.",
                                          "citations": [{"startIndex": 0}]},
                    "results": [{
                        "content": {"text": "TOTAL AUM: $19,217 MM"},
                        "metadata": {"_source_uri": "s3://bucket/fund.pdf"},
                        "sourceRetriever": {"identifier": "KB111"},
                    }],
                }},
            ])}

    monkeypatch.setattr(module, "_kb_runtime", FakeRuntime)
    result = asyncio.run(module.kb_deep_search.handler({"query": "aum", "kb_id": ""}))
    text = result["content"][0]["text"]

    assert calls[0]["messages"] == [{"role": "user", "content": {"text": "aum"}}]
    # one mounted KB → the single-KB iteration budget
    assert (
        calls[0]["agenticRetrieveConfiguration"]["maxAgentIteration"]
        == module.KB_DEEP_ITERATIONS_SINGLE
    )
    assert "planner steps: Planning:SUCCEEDED" in text
    assert "answer (1 citation(s)):" in text and "AUM is $10,706 MM." in text
    assert "kb=KB111" in text and "source=s3://bucket/fund.pdf" in text
    assert "ignored delta" not in text


def test_kb_deep_search_degrades_readably(rendered_kb_module, monkeypatch):
    module = rendered_kb_module

    class DenyingRuntime:
        def agentic_retrieve_stream(self, **_kwargs):
            return {"stream": iter([
                {"accessDeniedException": {"message": "bedrock:AgenticRetrieveStream"}}
            ])}

    monkeypatch.setattr(module, "_kb_runtime", DenyingRuntime)
    assert module.kb_deep_search_text("aum") == (
        "deep search failed: accessDeniedException: bedrock:AgenticRetrieveStream"
    )

    def _boom():
        raise RuntimeError("endpoint unreachable")

    monkeypatch.setattr(module, "_kb_runtime", _boom)
    assert module.kb_deep_search_text("aum").startswith(
        "deep search failed: RuntimeError:"
    )
    assert "not mounted" in module.kb_deep_search_text("aum", "KB999")
    assert "non-empty query" in module.kb_deep_search_text("  ")


def test_kb_search_tool_returns_formatted_passages(rendered_kb_module, monkeypatch):
    module = rendered_kb_module
    calls: list[dict] = []

    class FakeRuntime:
        def retrieve(self, **kwargs):
            calls.append(kwargs)
            return {
                "retrievalResults": [
                    {
                        "content": {"text": "Fund AUM was 1.2bn."},
                        "score": 0.4231,
                        "location": {"s3Location": {"uri": "s3://bucket/fund.pdf"}},
                    }
                ]
            }

    monkeypatch.setattr(module, "_kb_runtime", FakeRuntime)
    result = asyncio.run(module.kb_search.handler({"query": "aum", "kb_id": ""}))

    assert [c["knowledgeBaseId"] for c in calls] == ["KB111"]
    text = result["content"][0]["text"]
    assert "Fund AUM was 1.2bn." in text
    assert "score=0.4231" in text and "s3://bucket/fund.pdf" in text


def test_kb_search_degrades_readably(rendered_kb_module, monkeypatch):
    module = rendered_kb_module

    class DenyingRuntime:
        def retrieve(self, **_kwargs):
            raise RuntimeError("AccessDeniedException: bedrock:Retrieve")

    monkeypatch.setattr(module, "_kb_runtime", DenyingRuntime)
    text = module.kb_search_text("aum")
    assert text.startswith("[fund-docs] search failed: RuntimeError:")

    assert "not mounted" in module.kb_search_text("aum", "KB999")
    assert "non-empty query" in module.kb_search_text("  ")


def test_no_kb_agent_has_no_kb_server(tmp_path: Path, monkeypatch):
    module = _import_rendered(SPEC, tmp_path, monkeypatch, "rendered_claude_nokb_main")
    assert module._kb_mcp_servers() == {}
    assert module.build_options().mcp_servers in (None, {})
    for probe in (module.kb_search_text, module.kb_deep_search_text):
        assert probe("anything") == "no knowledge bases are mounted on this agent."


class FakeMemorySession:
    def __init__(self):
        self.turns = [
            [
                {"role": "USER", "content": {"text": "new question"}},
                {"role": "ASSISTANT", "content": {"text": "new answer"}},
            ],
            [
                {"role": "USER", "content": {"text": "old question"}},
                {"role": "ASSISTANT", "content": {"text": "old answer"}},
            ],
        ]
        self.search_calls: list[dict] = []
        self.saved: list[list] = []
        self.fail_reads = False
        self.fail_writes = False

    def get_last_k_turns(self, **kwargs):
        if self.fail_reads:
            raise RuntimeError("read failed with secret prompt")
        assert kwargs == {"k": 5}
        return self.turns

    def search_long_term_memories(self, **kwargs):
        if self.fail_reads:
            raise RuntimeError("read failed with secret prompt")
        self.search_calls.append(kwargs)
        namespace = kwargs["namespace"]
        if namespace.startswith("/facts/"):
            return [{"content": {"text": "customer has a standing appointment"}}]
        return [{"content": {"text": "prefers morning meetings"}}]

    def add_turns(self, *, messages):
        if self.fail_writes:
            raise RuntimeError("write failed with secret response")
        self.saved.append(messages)
        return {"eventId": "event-1"}


def _install_fake_memory_manager(module, monkeypatch):
    sessions: list[tuple[str, str, FakeMemorySession]] = []

    class FakeMemoryManager:
        def __init__(self, memory_id, region_name=None):
            assert memory_id == "memory-123"
            assert region_name == "us-west-2"

        def create_memory_session(self, *, actor_id, session_id):
            session = FakeMemorySession()
            sessions.append((actor_id, session_id, session))
            return session

    monkeypatch.setattr(module, "MemorySessionManager", FakeMemoryManager)
    return sessions


def test_memory_context_restores_history_and_long_term_records(
    rendered_memory_module, monkeypatch
):
    module = rendered_memory_module
    sessions = _install_fake_memory_manager(module, monkeypatch)
    memory = module.AgentCoreMemory("memory-123", "agent-a__river", "session-one")

    context = memory.context_for("What do you remember?")

    _, _, session = sessions[0]
    assert context.index("old question") < context.index("new question")
    assert "customer has a standing appointment" in context
    assert "prefers morning meetings" in context
    assert [call["namespace"] for call in session.search_calls] == [
        "/facts/agent-a__river",
        "/preferences/agent-a__river",
    ]
    assert all(call["query"] == "What do you remember?" for call in session.search_calls)
    assert len(context) <= module.MAX_MEMORY_CONTEXT_CHARS


def test_memory_scope_uses_exact_actor_and_session(
    rendered_memory_module, monkeypatch
):
    module = rendered_memory_module
    sessions = _install_fake_memory_manager(module, monkeypatch)

    module.AgentCoreMemory("memory-123", "agent-a__river", "session-one")
    module.AgentCoreMemory("memory-123", "agent-a__river", "session-two")
    module.AgentCoreMemory("memory-123", "agent-b__river", "session-one")

    assert [(actor, session) for actor, session, _ in sessions] == [
        ("agent-a__river", "session-one"),
        ("agent-a__river", "session-two"),
        ("agent-b__river", "session-one"),
    ]


def test_user_prompt_hook_returns_additional_context(rendered_memory_module):
    module = rendered_memory_module
    prompts: list[str] = []
    memory = SimpleNamespace(
        context_for=lambda prompt: prompts.append(prompt) or "remembered context"
    )

    result = asyncio.run(
        module._memory_hook(memory)(
            {"prompt": "original prompt"},
            None,
            {},
        )
    )

    assert prompts == ["original prompt"]
    assert result["hookSpecificOutput"] == {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "remembered context",
    }


def test_run_query_wires_request_local_memory_hook(
    rendered_memory_module, monkeypatch
):
    module = rendered_memory_module
    captured = {}
    memory = SimpleNamespace(context_for=lambda _prompt: "context")

    async def fake_query(*, prompt, options):
        captured["prompt"] = prompt
        captured["options"] = options
        if False:
            yield None

    monkeypatch.setattr(module, "query", fake_query)
    asyncio.run(module.run_query("original prompt", memory))

    assert captured["prompt"] == "original prompt"
    assert captured["options"].include_partial_messages is True
    matcher = captured["options"].hooks["UserPromptSubmit"][0]
    assert len(matcher.hooks) == 1


def test_query_events_stream_text_without_repeating_final_message(
    rendered_memory_module, monkeypatch
):
    module = rendered_memory_module
    captured = {}
    session_context: list[tuple[str, str]] = []

    @contextmanager
    def capture_session(session_id):
        session_context.append(("enter", session_id))
        try:
            yield
        finally:
            session_context.append(("exit", session_id))

    async def fake_query(*, prompt, options):
        assert session_context == [("enter", "runtime-session-one")]
        captured.update(prompt=prompt, options=options)
        yield module.StreamEvent(
            uuid="message-1",
            session_id="session-one",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hello "},
            },
        )
        yield module.StreamEvent(
            uuid="message-1",
            session_id="session-one",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "world"},
            },
        )
        yield module.AssistantMessage(
            content=[module.TextBlock(text="hello world")],
            model="test-model",
            uuid="message-1",
        )
        yield module.ResultMessage(
            subtype="success",
            duration_ms=10,
            duration_api_ms=8,
            is_error=False,
            num_turns=1,
            session_id="session-one",
            result="hello world",
        )

    monkeypatch.setattr(module, "using_session", capture_session)
    monkeypatch.setattr(module, "query", fake_query)
    outcome = module.QueryOutcome()

    events = asyncio.run(
        collect_async(
            module._query_events("hello", None, outcome, "runtime-session-one")
        )
    )

    assert events == [
        {"event": "delta", "text": "hello "},
        {"event": "delta", "text": "world"},
    ]
    assert outcome.result == "hello world"
    assert captured["options"].include_partial_messages is True
    assert session_context == [
        ("enter", "runtime-session-one"),
        ("exit", "runtime-session-one"),
    ]


def test_heartbeat_keeps_pending_query_event_alive(rendered_memory_module):
    module = rendered_memory_module

    async def exercise():
        release = asyncio.Event()

        async def delayed_events():
            await release.wait()
            yield {"event": "delta", "text": "still running"}

        events = module._events_with_heartbeat(delayed_events(), interval_s=0.001)
        heartbeat = await anext(events)
        release.set()
        delayed = await anext(events)
        with pytest.raises(StopAsyncIteration):
            await anext(events)
        return heartbeat, delayed

    heartbeat, delayed = asyncio.run(exercise())

    assert heartbeat["event"] == "heartbeat"
    assert isinstance(heartbeat["timestamp"], float)
    wire_frame = f"data: {json.dumps(heartbeat)}\n\n".encode()
    assert len(wire_frame) > rt.SSE_READ_CHUNK_BYTES
    assert delayed == {"event": "delta", "text": "still running"}


def test_heartbeat_resumes_source_generator_in_one_task(rendered_memory_module):
    module = rendered_memory_module
    task_ids: list[int] = []

    async def task_bound_events():
        task_ids.append(id(asyncio.current_task()))
        try:
            yield {"event": "delta", "text": "one"}
            await asyncio.sleep(0)
            task_ids.append(id(asyncio.current_task()))
            yield {"event": "delta", "text": "two"}
        finally:
            task_ids.append(id(asyncio.current_task()))

    events = asyncio.run(
        collect_async(module._events_with_heartbeat(task_bound_events(), interval_s=1))
    )

    assert events == [
        {"event": "delta", "text": "one"},
        {"event": "delta", "text": "two"},
    ]
    assert len(set(task_ids)) == 1


def test_heartbeat_producer_keeps_bounded_backpressure(rendered_memory_module):
    module = rendered_memory_module

    async def exercise():
        produced: list[int] = []

        async def source():
            for index in range(4):
                produced.append(index)
                yield {"event": "delta", "text": str(index)}

        events = module._events_with_heartbeat(source(), interval_s=1)
        first = await anext(events)
        await asyncio.sleep(0)
        ahead = list(produced)
        rest = [event async for event in events]
        return first, ahead, rest

    first, ahead, rest = asyncio.run(exercise())

    assert first == {"event": "delta", "text": "0"}
    assert len(ahead) < 4
    assert rest == [
        {"event": "delta", "text": "1"},
        {"event": "delta", "text": "2"},
        {"event": "delta", "text": "3"},
    ]


def test_local_smoke_traps_credential_file_before_export():
    smoke = Path("../scripts/local_container_smoke.sh").read_text()
    assert smoke.index("trap cleanup EXIT") < smoke.index(
        "aws configure export-credentials"
    )
    assert '[[ -z "$ENVFILE" ]] || rm -f "$ENVFILE"' in smoke


def test_invoke_persists_completed_turn_once(rendered_memory_module, monkeypatch):
    module = rendered_memory_module
    saved: list[tuple[str, str]] = []
    memory = SimpleNamespace(save_turn=lambda prompt, response: saved.append((prompt, response)))
    monkeypatch.setattr(module, "_create_memory", lambda actor, session: memory)

    async def fake_query_events(prompt, request_memory, outcome, session_id):
        assert prompt == "hello"
        assert request_memory is memory
        assert session_id == "session-one"
        outcome.result = "hello back"
        outcome.usage = {"input_tokens": 2}
        yield {"event": "delta", "text": "hello back"}

    monkeypatch.setattr(module, "_query_events", fake_query_events)
    events = asyncio.run(
        collect_async(module.invoke(
            {"prompt": "hello", "actor_id": "agent-a__river"},
            SimpleNamespace(session_id="session-one"),
        ))
    )

    assert events == [
        {"event": "delta", "text": "hello back"},
        {
            "event": "complete",
            "result": "hello back",
            "usage": {"input_tokens": 2},
        },
    ]
    assert saved == [("hello", "hello back")]


def test_save_turn_writes_one_user_assistant_event(
    rendered_memory_module, monkeypatch
):
    module = rendered_memory_module
    sessions = _install_fake_memory_manager(module, monkeypatch)
    memory = module.AgentCoreMemory("memory-123", "agent-a__river", "session-one")

    assert memory.save_turn("hello", "hello back") is True

    (messages,) = sessions[0][2].saved
    assert [(message.text, message.role) for message in messages] == [
        ("hello", module.MessageRole.USER),
        ("hello back", module.MessageRole.ASSISTANT),
    ]


def test_query_failure_does_not_persist_turn(rendered_memory_module, monkeypatch):
    module = rendered_memory_module
    saved: list[tuple[str, str]] = []
    memory = SimpleNamespace(save_turn=lambda prompt, response: saved.append((prompt, response)))
    monkeypatch.setattr(module, "_create_memory", lambda actor, session: memory)

    async def failed_query(_prompt, _memory, _outcome, _session_id):
        raise RuntimeError("claude failed")
        yield

    monkeypatch.setattr(module, "_query_events", failed_query)
    with pytest.raises(RuntimeError, match="claude failed"):
        asyncio.run(
            collect_async(module.invoke(
                {"prompt": "hello", "actor_id": "agent-a__river"},
                SimpleNamespace(session_id="session-one"),
            ))
        )
    assert saved == []


def test_memory_failures_warn_without_leaking_content(
    rendered_memory_module, monkeypatch, caplog
):
    module = rendered_memory_module
    sessions = _install_fake_memory_manager(module, monkeypatch)
    memory = module.AgentCoreMemory("memory-123", "agent-a__river", "session-one")
    session = sessions[0][2]
    session.fail_reads = True

    assert memory.context_for("secret prompt") == ""
    session.fail_writes = True
    assert memory.save_turn("secret prompt", "secret response") is False

    assert "short-term retrieval failed for session session-one" in caplog.text
    assert "long-term retrieval failed for session session-one" in caplog.text
    assert "persistence failed for session session-one" in caplog.text
    assert "secret prompt" not in caplog.text
    assert "secret response" not in caplog.text


def test_memory_disabled_or_missing_actor_skips_manager(
    rendered_memory_module, monkeypatch
):
    module = rendered_memory_module

    class UnexpectedManager:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("memory manager should not be created")

    monkeypatch.setattr(module, "MemorySessionManager", UnexpectedManager)
    monkeypatch.setattr(module, "MEMORY_SHORT_TERM", False)
    monkeypatch.setattr(module, "MEMORY_LONG_TERM", False)
    assert module._create_memory("agent-a__river", "session-one") is None

    monkeypatch.setattr(module, "MEMORY_SHORT_TERM", True)
    assert module._create_memory("", "session-one") is None
    monkeypatch.setattr(module, "MEMORY_ID", "")
    assert module._create_memory("agent-a__river", "session-one") is None


class StubCodeBuild:
    def __init__(self, phases_then_status):
        self.script = list(phases_then_status)
        self.started_with = None

    def start_build(self, **kwargs):
        self.started_with = kwargs
        return {"build": {"id": "launchpad-agent-builder:abc123"}}

    def batch_get_builds(self, ids):
        phase, status = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        return {
            "builds": [
                {
                    "id": ids[0],
                    "currentPhase": phase,
                    "buildStatus": status,
                    "phases": [
                        {
                            "phaseType": "BUILD",
                            "phaseStatus": "FAILED" if status == "FAILED" else "SUCCEEDED",
                            "contexts": [{"message": "docker build exited 1"}],
                        }
                    ],
                }
            ]
        }


def test_start_image_build_payload():
    stub = StubCodeBuild([("SUBMITTED", "IN_PROGRESS")])
    build_id = cb.start_image_build(
        stub,
        project="launchpad-agent-builder",
        s3_bucket="bkt",
        s3_key="builds/a/source.zip",
        region="us-west-2",
        ecr_registry="111.dkr.ecr.us-west-2.amazonaws.com",
        ecr_repo="launchpad-agents",
        image_tag="a-v1",
    )
    assert build_id == "launchpad-agent-builder:abc123"
    assert stub.started_with["sourceLocationOverride"] == "bkt/builds/a/source.zip"
    env = {e["name"]: e["value"] for e in stub.started_with["environmentVariablesOverride"]}
    assert env["IMAGE_TAG"] == "a-v1"
    assert env["ECR_REPO"] == "launchpad-agents"


def test_wait_build_streams_phases_to_succeeded():
    stub = StubCodeBuild(
        [
            ("SUBMITTED", "IN_PROGRESS"),
            ("PRE_BUILD", "IN_PROGRESS"),
            ("BUILD", "IN_PROGRESS"),
            ("COMPLETED", "SUCCEEDED"),
        ]
    )
    phases: list[str] = []
    build = cb.wait_build(stub, "b-1", sleeper=lambda _: None, on_phase=phases.append)
    assert build["buildStatus"] == "SUCCEEDED"
    assert phases == ["SUBMITTED", "PRE_BUILD", "BUILD", "COMPLETED"]


def test_wait_build_raises_on_failed_with_context():
    stub = StubCodeBuild([("BUILD", "IN_PROGRESS"), ("COMPLETED", "FAILED")])
    with pytest.raises(RuntimeError, match="docker build exited 1"):
        cb.wait_build(stub, "b-1", sleeper=lambda _: None)


class StubControl:
    def __init__(self):
        self.created_with = None

    def create_agent_runtime(self, **kwargs):
        self.created_with = kwargs
        return {"agentRuntimeId": "rt-c1", "agentRuntimeArn": "arn:rt-c1", "status": "CREATING"}


def test_create_container_runtime_payload():
    stub = StubControl()
    rt.create_container_runtime(
        stub,
        runtime_name="sdk_test_abc123",
        container_uri="111.dkr.ecr.us-west-2.amazonaws.com/launchpad-agents:a-v1",
        role_arn="arn:role",
    )
    artifact = stub.created_with["agentRuntimeArtifact"]
    assert artifact == {
        "containerConfiguration": {
            "containerUri": "111.dkr.ecr.us-west-2.amazonaws.com/launchpad-agents:a-v1"
        }
    }
