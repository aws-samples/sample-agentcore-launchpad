"""Template rendering, compilation, and the config-bundle fallback contract."""

import importlib.util
import py_compile
import types
from pathlib import Path

import pytest
import strands

from app.schemas.agent import DEFAULT_MODEL_ID, AgentSpec
from app.templates.strands_agent import base_requirements, render_main_py

SPEC = AgentSpec(
    name="tmpl-test-agent",
    method="zip_runtime",
    system_prompt="You are a template test agent. Be brief.",
)


def test_render_replaces_all_placeholders():
    code = render_main_py(SPEC)
    assert "__LAUNCHPAD_" not in code
    assert "tmpl-test-agent" in code
    assert DEFAULT_MODEL_ID in code
    assert "You are a template test agent. Be brief." in code
    assert "get_config_bundle" in code
    assert "BedrockAgentCoreApp" in code
    assert "AgentCoreMemorySessionManager" in code
    assert "create_event(" not in code


def test_rendered_template_compiles(tmp_path: Path):
    target = tmp_path / "main.py"
    target.write_text(render_main_py(SPEC), encoding="utf-8")
    py_compile.compile(str(target), doraise=True)  # raises on syntax error


def test_base_requirements_include_contract_deps():
    reqs = base_requirements()
    joined = " ".join(reqs)
    assert "strands-agents" in joined
    assert "bedrock-agentcore" in joined
    assert "aws-opentelemetry-distro" in joined


class _FakeTool:
    def __init__(self, fn):
        self.fn = fn
        self.tool_name = fn.__name__
        self.tool_spec = {"description": fn.__doc__}

    def __call__(self, *args, **kwargs):
        return self.fn(*args, **kwargs)


@pytest.fixture
def template_module(tmp_path: Path, monkeypatch):
    """Import the rendered template with a stubbed strands module."""
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    monkeypatch.setattr(strands, "tool", _FakeTool)

    target = tmp_path / "rendered_main.py"
    target.write_text(render_main_py(SPEC), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("rendered_main", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_defaults_apply_without_bundle(template_module, monkeypatch):
    monkeypatch.setattr(
        template_module.BedrockAgentCoreContext, "get_config_bundle", staticmethod(lambda: {})
    )
    assert template_module.resolve_system_prompt() == SPEC.system_prompt
    default_desc = template_module.DEFAULT_TOOL_DESCRIPTIONS["calculator"]
    assert template_module.resolve_tool_description("calculator") == default_desc


def test_bundle_overrides_prompt_and_tool_descriptions(template_module, monkeypatch):
    bundle = {
        "system_prompt": "OVERRIDDEN prompt from treatment bundle",
        "tool_descriptions": {"calculator": "OVERRIDDEN calculator description"},
    }
    monkeypatch.setattr(
        template_module.BedrockAgentCoreContext,
        "get_config_bundle",
        staticmethod(lambda: bundle),
    )
    assert template_module.resolve_system_prompt() == bundle["system_prompt"]
    assert (
        template_module.resolve_tool_description("calculator")
        == "OVERRIDDEN calculator description"
    )
    # unlisted tools still fall back to defaults
    assert (
        template_module.resolve_tool_description("current_utc_time")
        == template_module.DEFAULT_TOOL_DESCRIPTIONS["current_utc_time"]
    )


def test_documented_bundle_tool_shape_overrides_legacy(template_module, monkeypatch):
    bundle = {
        "tool_descriptions": {"calculator": "legacy description"},
        "tools": {"calculator": {"description": "documented description"}},
    }
    monkeypatch.setattr(
        template_module.BedrockAgentCoreContext,
        "get_config_bundle",
        staticmethod(lambda: bundle),
    )
    assert (
        template_module.resolve_tool_description("calculator")
        == "documented description"
    )


def test_promoted_tool_defaults_are_rendered(tmp_path: Path, monkeypatch):
    spec_with_defaults = SPEC.model_copy(update={
        "tool_description_overrides": {"calculator": "promoted description"},
    })
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    monkeypatch.setattr(strands, "tool", _FakeTool)
    target = tmp_path / "promoted_main.py"
    target.write_text(render_main_py(spec_with_defaults), encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location("promoted_main", target)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    monkeypatch.setattr(
        module.BedrockAgentCoreContext,
        "get_config_bundle",
        staticmethod(lambda: {}),
    )
    assert module.resolve_tool_description("calculator") == "promoted description"


def test_template_tools_work(template_module):
    assert template_module.calculator("2+2*3") == "8"
    assert template_module.current_utc_time().startswith("20")


def test_build_agent_omits_session_manager_without_memory(template_module, monkeypatch):
    monkeypatch.setattr(template_module, "MEMORY_ID", "")
    agent = template_module.build_agent("agent__river", "session-one")
    assert not hasattr(agent, "session_manager")


def test_build_agent_scopes_memory_to_actor_and_session(template_module, monkeypatch):
    captured = {}

    class FakeManager:
        def __init__(self, config, region_name=None):
            captured["config"] = config
            captured["region_name"] = region_name

    monkeypatch.setattr(template_module, "MEMORY_ID", "memory-123")
    monkeypatch.setattr(template_module, "AgentCoreMemorySessionManager", FakeManager)
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    agent = template_module.build_agent("agent__river", "session-one")

    assert isinstance(agent.session_manager, FakeManager)
    assert captured["config"].memory_id == "memory-123"
    assert captured["config"].actor_id == "agent__river"
    assert captured["config"].session_id == "session-one"
    assert captured["region_name"] == "us-west-2"
