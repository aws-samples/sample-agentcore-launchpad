"""Template rendering, compilation, and the config-bundle fallback contract."""

import importlib.util
import py_compile
import sys
import types
from contextlib import ExitStack
from pathlib import Path

import pytest
import strands

from app.schemas.agent import DEFAULT_MODEL_ID, AgentSpec, ToolRef
from app.templates.gateway_support import render_gateway_source
from app.templates.kb_support import mounted_kbs, render_direct_kb_source
from app.templates.strands_agent import base_requirements, render_main_py

SPEC = AgentSpec(
    name="tmpl-test-agent",
    method="zip_runtime",
    system_prompt="You are a template test agent. Be brief.",
)

MANTLE_MODEL_ID = "openai.gpt-5.6-sol"
MANTLE_SPEC = SPEC.model_copy(
    update={"model_source": "mantle", "model_id": MANTLE_MODEL_ID}
)
SKILL_SPEC = SPEC.model_copy(
    update={"skills": ["s3://launchpad-artifacts/skills/pirate-speak/"]}
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
    assert "SKILLS_ENABLED = False" in code


def test_render_preserves_skills_placeholder_literal_in_prompt():
    spec = SPEC.model_copy(
        update={"system_prompt": "Keep __LAUNCHPAD_SKILLS_ENABLED__ literal."}
    )

    code = render_main_py(spec)

    assert "DEFAULT_SYSTEM_PROMPT = 'Keep __LAUNCHPAD_SKILLS_ENABLED__ literal.'" in code
    assert "SKILLS_ENABLED = False" in code


def test_rendered_template_compiles(tmp_path: Path):
    target = tmp_path / "main.py"
    target.write_text(render_main_py(SPEC), encoding="utf-8")
    py_compile.compile(str(target), doraise=True)  # raises on syntax error


def test_mantle_source_renders_a_model_object_and_no_api_key(tmp_path: Path):
    code = render_main_py(MANTLE_SPEC)
    assert "__LAUNCHPAD_" not in code
    assert f'MODEL_ID = "{MANTLE_MODEL_ID}"' in code
    assert 'MODEL_SOURCE = "mantle"' in code
    assert "OpenAIResponsesModel" in code
    assert "bedrock_mantle_config" in code
    # Mantle auth is IAM-only: the SDK mints a bearer token from the execution
    # role per request, so no key may leak into the generated agent.
    assert "BEDROCK_API_KEY" not in code
    assert "api_key" not in code
    target = tmp_path / "mantle_main.py"
    target.write_text(code, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)


def test_bedrock_source_still_wires_a_bare_model_id_string():
    """Regression: the default source must reach strands exactly as before."""
    code = render_main_py(SPEC)
    assert 'MODEL_SOURCE = "bedrock"' in code
    assert f'MODEL_ID = "{DEFAULT_MODEL_ID}"' in code
    # `Agent(model=<bare id>)` resolves to a Bedrock Converse call. Anything
    # other than the plain string here silently changes every existing agent.
    assert '"model": build_model(),' in code


def _mantle_module(tmp_path: Path, monkeypatch, spec=MANTLE_SPEC):
    """Import a rendered Mantle agent with a stubbed OpenAIResponsesModel.

    `strands.models.openai_responses` reads the `openai` package version at
    import time and `openai` is only present via the `openai` extra, so the real
    module cannot be imported in this test environment — which is exactly why the
    template's import is function-local.
    """
    captured: dict = {}

    class FakeOpenAIResponsesModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("strands.models.openai_responses")
    module.OpenAIResponsesModel = FakeOpenAIResponsesModel
    monkeypatch.setitem(sys.modules, "strands.models.openai_responses", module)
    return _import_rendered(spec, tmp_path, monkeypatch, "mantle_agent"), captured


def test_mantle_build_model_mints_from_iam_in_the_model_region(tmp_path: Path, monkeypatch):
    module, captured = _mantle_module(tmp_path, monkeypatch)
    model = module.build_model()
    assert type(model).__name__ == "FakeOpenAIResponsesModel"
    # us-east-1, NOT AWS_REGION: the runtime lives in us-west-2, where these
    # models are not offered.
    assert captured == {
        "bedrock_mantle_config": {"region": "us-east-1"},
        "model_id": MANTLE_MODEL_ID,
    }
    # the Agent gets a model OBJECT, not the bare id a Converse agent gets
    assert type(module.build_agent("a", "s").model).__name__ == "FakeOpenAIResponsesModel"


def test_mantle_region_is_overridable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_MANTLE_REGION", "eu-central-1")
    module, captured = _mantle_module(tmp_path, monkeypatch)
    assert module.MANTLE_REGION == "eu-central-1"
    assert captured == {}  # build_model is lazy — nothing is constructed at import
    module.build_model()
    assert captured["bedrock_mantle_config"] == {"region": "eu-central-1"}


def test_bedrock_build_model_returns_the_bare_id(template_module):
    model = template_module.build_model()
    assert model == DEFAULT_MODEL_ID
    assert isinstance(model, str)  # a bare id ⇒ Bedrock Converse
    assert template_module.build_agent("a", "s").model == DEFAULT_MODEL_ID


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


KB_SPEC = AgentSpec(**{
    **SPEC.model_dump(),
    "knowledge_bases": [
        {"kb_id": "KB111", "name": "fund-docs", "description": "fund product PDFs"},
        {"kb_id": "KB222", "name": "faq", "description": ""},
    ],
})


def _import_rendered(spec, tmp_path: Path, monkeypatch, stem: str):
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    monkeypatch.setattr(strands, "tool", _FakeTool)
    target = tmp_path / f"{stem}.py"
    target.write_text(render_main_py(spec), encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location(stem, target)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_skill_plugin_requires_enabled_spec_and_packaged_skill(tmp_path: Path, monkeypatch):
    class FakeAgentSkills:
        def __init__(self, *, skills):
            self.skills = skills

    monkeypatch.setattr(strands, "AgentSkills", FakeAgentSkills)
    module = _import_rendered(SKILL_SPEC, tmp_path, monkeypatch, "skill_main")

    assert module.SKILLS_ENABLED is True
    assert module.skill_plugins() == []
    assert not hasattr(module.build_agent("a", "s"), "plugins")

    skill_dir = tmp_path / "skills" / "pirate-speak"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Pirate speech", encoding="utf-8")

    plugins = module.skill_plugins()
    assert len(plugins) == 1
    assert plugins[0].skills == str(tmp_path / "skills")
    assert module.build_agent("a", "s").plugins[0].skills == str(tmp_path / "skills")


@pytest.fixture
def kb_module(tmp_path: Path, monkeypatch):
    return _import_rendered(KB_SPEC, tmp_path, monkeypatch, "kb_main")


class _FakeRuntime:
    """Stub bedrock-agent-runtime: KB222 always blows up, KB111 returns a hit.

    ``agentic_retrieve_stream`` replays the event sequence a live call produced on
    2026-07-28 (trace steps → result); ``deep_stream`` can be swapped to exercise
    the in-stream error members.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.deep_calls: list[dict] = []
        self.deep_stream: list[dict] | None = None

    def retrieve(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["knowledgeBaseId"] == "KB222":
            raise RuntimeError("index not ready")
        return {
            "retrievalResults": [
                {
                    "content": {"text": "Fund AUM was 1.2bn as of Aug 2021."},
                    "score": 0.4231,
                    "location": {"s3Location": {"uri": "s3://bucket/fund.pdf"}},
                }
            ]
        }

    def agentic_retrieve_stream(self, **kwargs):
        self.deep_calls.append(kwargs)
        events = self.deep_stream
        if events is None:
            events = [
                {"traceEvent": {"attributes": {
                    "step": "SpeculativeRetrieval", "status": "SUCCEEDED"}}},
                {"traceEvent": {"attributes": {"step": "Planning", "status": "SUCCEEDED"}}},
                # answer deltas the tool must ignore (result carries the full text)
                {"responseEvent": {"text": "Global "}},
                {"responseEvent": {"text": "Emerging"}},
                {"result": {
                    "generatedResponse": {
                        "answer": "Global Emerging Markets is $10,706 MM.",
                        "citations": [{"startIndex": 0}, {"startIndex": 9}],
                    },
                    "results": [{
                        "content": {"text": "TOTAL AUM: $19,217 MM"},
                        "metadata": {"_source_uri": "s3://bucket/fund.pdf",
                                     "_document_title": "MS Oct 21"},
                        "sourceRetriever": {"identifier": "KB111"},
                    }],
                }},
            ]
        return {"stream": iter(events)}


def test_kb_render_bakes_refs_and_prompt_section():
    code = render_main_py(KB_SPEC)
    assert "__LAUNCHPAD_" not in code
    for token in ("KB111", "KB222", "fund-docs", "fund product PDFs"):
        assert token in code
    assert "## Knowledge bases" in code
    # the section must steer between both tools, not just announce one
    assert "`kb_search`" in code and "`kb_deep_search`" in code
    # description-less KB falls back to its name
    assert "faq (kb_id `KB222`) — faq" in code


def test_kb_render_inlines_the_reusable_direct_source(tmp_path: Path):
    direct_source = render_direct_kb_source(mounted_kbs(KB_SPEC))
    assert direct_source in render_main_py(KB_SPEC)
    assert "__LAUNCHPAD_" not in direct_source
    assert "def kb_search(" in direct_source
    assert "def kb_deep_search(" in direct_source
    assert "One similarity search — fast and cheap." in direct_source
    assert "agentic (multi-step) retrieval" in direct_source
    assert "Slower and more expensive than kb_search" in direct_source
    target = tmp_path / "launchpad_kb_tools.py"
    target.write_text(direct_source, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)


def test_kb_tools_registered_only_when_mounted(kb_module, template_module):
    assert kb_module.MOUNTED_KBS
    assert "One similarity search — fast and cheap." in kb_module.kb_search.tool_spec[
        "description"
    ]
    assert "agentic (multi-step) retrieval" in kb_module.kb_deep_search.tool_spec[
        "description"
    ]
    assert "Slower and more expensive than kb_search" in (
        kb_module.kb_deep_search.tool_spec["description"]
    )
    kb_names = [t.tool_name for t in kb_module.build_agent("a", "s").tools]
    assert "kb_search" in kb_names and "kb_deep_search" in kb_names
    # both descriptions are A/B-tunable through the config-bundle contract
    assert kb_module.DEFAULT_TOOL_DESCRIPTIONS["kb_search"].startswith("Search the mounted")
    assert kb_module.DEFAULT_TOOL_DESCRIPTIONS["kb_deep_search"].startswith("Deep-search")

    assert template_module.MOUNTED_KBS == []
    plain = [t.tool_name for t in template_module.build_agent("a", "s").tools]
    assert "kb_search" not in plain and "kb_deep_search" not in plain
    assert "kb_search" not in template_module.DEFAULT_TOOL_DESCRIPTIONS
    assert "kb_deep_search" not in template_module.DEFAULT_TOOL_DESCRIPTIONS
    assert template_module.resolve_system_prompt() == SPEC.system_prompt


def test_kb_search_fans_out_over_every_mounted_kb(kb_module, monkeypatch):
    runtime = _FakeRuntime()
    monkeypatch.setattr(kb_module, "_kb_runtime", lambda: runtime)
    out = kb_module.kb_search("what was the AUM?")
    assert [c["knowledgeBaseId"] for c in runtime.calls] == ["KB111", "KB222"]
    assert runtime.calls[0]["retrievalConfiguration"] == {
        "managedSearchConfiguration": {"numberOfResults": kb_module.KB_RESULTS}
    }
    assert "Fund AUM was 1.2bn" in out
    assert "score=0.4231" in out
    assert "s3://bucket/fund.pdf" in out
    # a broken KB degrades to a line, it does not raise
    assert "[faq] search failed: RuntimeError: index not ready" in out


def test_kb_search_targets_one_kb_and_rejects_unknown_ids(kb_module, monkeypatch):
    runtime = _FakeRuntime()
    monkeypatch.setattr(kb_module, "_kb_runtime", lambda: runtime)

    kb_module.kb_search("aum", "KB111")
    assert [c["knowledgeBaseId"] for c in runtime.calls] == ["KB111"]

    out = kb_module.kb_search("aum", "KB999")
    assert "not mounted" in out and "KB111, KB222" in out
    assert len(runtime.calls) == 1  # no request issued for an unmounted id

    assert "non-empty query" in kb_module.kb_search("   ")


def test_kb_search_reports_empty_results(kb_module, monkeypatch):
    monkeypatch.setattr(
        kb_module, "_kb_runtime", lambda: types.SimpleNamespace(retrieve=lambda **_: {})
    )
    assert kb_module.kb_search("aum", "KB111") == "[fund-docs] no matching passages."


def test_kb_deep_search_returns_answer_citations_and_passages(kb_module, monkeypatch):
    runtime = _FakeRuntime()
    monkeypatch.setattr(kb_module, "_kb_runtime", lambda: runtime)

    out = kb_module.kb_deep_search("compare the two strategies")

    (call,) = runtime.deep_calls
    # content is a STRUCT, not a list — the AWS blog example's shape is wrong
    assert call["messages"] == [
        {"role": "user", "content": {"text": "compare the two strategies"}}
    ]
    assert [r["configuration"]["knowledgeBase"]["knowledgeBaseId"] for r in call["retrievers"]] == [
        "KB111",
        "KB222",
    ]
    cfg = call["agenticRetrieveConfiguration"]
    assert cfg["foundationModelType"] == "MANAGED"
    assert cfg["rerankingModelType"] == "MANAGED"
    # two retrievers → the multi-KB iteration budget
    assert cfg["maxAgentIteration"] == kb_module.KB_DEEP_ITERATIONS_MULTI

    assert "planner steps: SpeculativeRetrieval:SUCCEEDED, Planning:SUCCEEDED" in out
    assert "answer (2 citation(s)):" in out
    assert "Global Emerging Markets is $10,706 MM." in out
    assert "supporting passages (1):" in out
    # agentic results carry no score/location — kb id + metadata uri instead
    assert "kb=KB111" in out and "source=s3://bucket/fund.pdf" in out
    # the responseEvent deltas must not be duplicated into the output
    assert out.count("Global Emerging") == 1


def test_kb_deep_search_single_kb_uses_smaller_iteration_budget(kb_module, monkeypatch):
    runtime = _FakeRuntime()
    monkeypatch.setattr(kb_module, "_kb_runtime", lambda: runtime)
    kb_module.kb_deep_search("aum", "KB111")
    cfg = runtime.deep_calls[0]["agenticRetrieveConfiguration"]
    assert cfg["maxAgentIteration"] == kb_module.KB_DEEP_ITERATIONS_SINGLE


def test_kb_deep_search_surfaces_in_stream_error_members(kb_module, monkeypatch):
    """Modeled errors arrive INSIDE the event stream, not only as raises."""
    runtime = _FakeRuntime()
    runtime.deep_stream = [
        {"accessDeniedException": {"message": "not authorized to AgenticRetrieveStream"}}
    ]
    monkeypatch.setattr(kb_module, "_kb_runtime", lambda: runtime)
    out = kb_module.kb_deep_search("aum")
    assert out == (
        "deep search failed: accessDeniedException: "
        "not authorized to AgenticRetrieveStream"
    )


def test_kb_deep_search_reports_trace_failures_and_missing_result(kb_module, monkeypatch):
    runtime = _FakeRuntime()
    runtime.deep_stream = [
        {"traceEvent": {"attributes": {
            "step": "Retrieval", "status": "FAILED",
            "failures": [{"message": "retriever timed out"}],
        }}},
    ]
    monkeypatch.setattr(kb_module, "_kb_runtime", lambda: runtime)
    assert kb_module.kb_deep_search("aum") == "deep search returned no result event."

    runtime.deep_stream = runtime.deep_stream + [{"result": {"results": []}}]
    out = kb_module.kb_deep_search("aum")
    assert "note: retriever timed out" in out
    assert "no supporting passages returned." in out


def test_kb_deep_search_degrades_on_raise_and_bad_id(kb_module, monkeypatch):
    def _boom():
        raise RuntimeError("endpoint unreachable")

    monkeypatch.setattr(kb_module, "_kb_runtime", _boom)
    assert kb_module.kb_deep_search("aum") == (
        "deep search failed: RuntimeError: endpoint unreachable"
    )

    runtime = _FakeRuntime()
    monkeypatch.setattr(kb_module, "_kb_runtime", lambda: runtime)
    out = kb_module.kb_deep_search("aum", "KB999")
    assert "not mounted" in out and "KB111, KB222" in out
    assert runtime.deep_calls == []  # no request issued for an unmounted id
    assert "non-empty query" in kb_module.kb_deep_search("   ")


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


# --- platform toolkits ------------------------------------------------------

TOOLKIT_SPEC = SPEC.model_copy(update={"toolkits": ["hr_assistant"]})
TOOLKIT_KB_SPEC = KB_SPEC.model_copy(update={"toolkits": ["hr_assistant"]})
HR_TOOL_NAMES = [
    "get_pto_balance",
    "submit_pto_request",
    "lookup_hr_policy",
    "get_benefits_summary",
    "get_pay_stub",
]


@pytest.fixture
def toolkit_module(tmp_path: Path, monkeypatch):
    return _import_rendered(TOOLKIT_SPEC, tmp_path, monkeypatch, "toolkit_main")


def test_no_toolkit_leaves_the_tool_list_and_source_untouched(template_module):
    """The additions must be inert for every spec written before toolkits existed."""
    code = render_main_py(SPEC)
    assert "__LAUNCHPAD_" not in code
    assert "TOOLKIT_TOOLS = []" in code
    assert "toolkit: hr_assistant" not in code
    assert template_module.TOOLKIT_TOOLS == []
    names = [t.tool_name for t in template_module.build_agent("a", "s").tools]
    assert names == ["calculator", "current_utc_time"]


def test_toolkit_renders_its_source_and_compiles(tmp_path: Path):
    code = render_main_py(TOOLKIT_SPEC)
    assert "__LAUNCHPAD_" not in code
    for name in HR_TOOL_NAMES:
        assert f"def {name}(" in code
    # seed data travels with the functions
    assert '"EMP-001": {"total_days": 15, "used_days": 5, "remaining_days": 10}' in code
    target = tmp_path / "toolkit_main.py"
    target.write_text(code, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)


def test_toolkit_brings_no_model_of_its_own(toolkit_module):
    """The upstream sample hardcodes BedrockModel("us.amazon.nova-lite-v1:0").

    Asserted against the imported module rather than the source text, because the
    toolkit's provenance comment legitimately names what was dropped.
    """
    assert toolkit_module.MODEL_ID == DEFAULT_MODEL_ID
    assert not hasattr(toolkit_module, "BedrockModel")
    assert not hasattr(toolkit_module, "_MODEL")


def test_toolkit_replaces_the_template_tools(toolkit_module):
    names = [t.tool_name for t in toolkit_module.build_agent("a", "s").tools]
    assert names == HR_TOOL_NAMES
    # still defined, just unregistered — same shape as an unmounted kb_search
    assert callable(toolkit_module.calculator)
    assert callable(toolkit_module.current_utc_time)


def test_toolkit_tools_are_config_bundle_tunable(toolkit_module, monkeypatch):
    for name in HR_TOOL_NAMES:
        assert toolkit_module.DEFAULT_TOOL_DESCRIPTIONS[name]
    monkeypatch.setattr(
        toolkit_module.BedrockAgentCoreContext,
        "get_config_bundle",
        staticmethod(lambda: {"tool_descriptions": {"get_pay_stub": "TREATMENT DESC"}}),
    )
    assert toolkit_module.resolve_tool_description("get_pay_stub") == "TREATMENT DESC"
    # untouched tools keep their rendered default
    assert toolkit_module.resolve_tool_description("get_pto_balance").startswith(
        "Return the current PTO balance"
    )
    specs = {t.tool_name: t.tool_spec["description"] for t in
             toolkit_module.build_agent("a", "s").tools}
    assert specs["get_pay_stub"] == "TREATMENT DESC"


def test_promoted_overrides_beat_the_toolkit_default(tmp_path: Path, monkeypatch):
    spec = TOOLKIT_SPEC.model_copy(
        update={"tool_description_overrides": {"get_pay_stub": "PROMOTED"}}
    )
    module = _import_rendered(spec, tmp_path, monkeypatch, "promoted_toolkit_main")
    assert module.DEFAULT_TOOL_DESCRIPTIONS["get_pay_stub"] == "PROMOTED"


def test_toolkit_keeps_kb_tools_when_kbs_are_mounted(tmp_path: Path, monkeypatch):
    module = _import_rendered(TOOLKIT_KB_SPEC, tmp_path, monkeypatch, "toolkit_kb_main")
    names = [t.tool_name for t in module.build_agent("a", "s").tools]
    assert names == [*HR_TOOL_NAMES, "kb_search", "kb_deep_search"]


def test_toolkit_seed_data_is_deterministic(toolkit_module):
    assert toolkit_module.get_pto_balance("EMP-001")["remaining_days"] == 10
    assert toolkit_module.get_pto_balance("EMP-001") == toolkit_module.get_pto_balance("EMP-001")
    first = toolkit_module.submit_pto_request("EMP-001", "2026-06-01", "2026-06-05")
    second = toolkit_module.submit_pto_request("EMP-001", "2026-06-01", "2026-06-05")
    assert first == second
    assert first["request_id"].startswith("PTO-2026-")
    other = toolkit_module.submit_pto_request("EMP-001", "2026-07-01", "2026-07-05")
    assert other["request_id"] != first["request_id"]


def test_toolkit_errors_survive_for_the_failure_modes(toolkit_module):
    """The prompt-fixable defects need tools that ERROR — keep that surface."""
    assert "error" in toolkit_module.get_pto_balance("EMP-999")
    assert "error" in toolkit_module.lookup_hr_policy("dress_code")
    assert "error" in toolkit_module.get_benefits_summary("pet_insurance")
    assert "error" in toolkit_module.get_pay_stub("EMP-002", "2026-01")
    # and tools that succeed must still succeed
    assert "401(k) Plan" in toolkit_module.get_benefits_summary("401k")["summary"]
    assert toolkit_module.get_pay_stub("EMP-001", "2026-01")["net_pay"] == 5362.50


# --- gateway MCP tools ------------------------------------------------------

GATEWAY_SPEC = SPEC.model_copy(
    update={"tools": [ToolRef(type="gateway", name="launchpad-gw")]}
)


@pytest.fixture
def gateway_module(tmp_path: Path, monkeypatch):
    return _import_rendered(GATEWAY_SPEC, tmp_path, monkeypatch, "gateway_main")


def test_no_gateway_tool_renders_a_no_op_loader(template_module):
    code = render_main_py(SPEC)
    assert "__LAUNCHPAD_" not in code
    assert "GATEWAY_TOOLS = lambda _stack: []" in code
    assert "launchpad-gateway-mcp" not in code
    assert template_module.GATEWAY_TOOLS(ExitStack()) == []


def test_gateway_spec_renders_the_client_and_compiles(tmp_path: Path):
    code = render_main_py(GATEWAY_SPEC)
    assert "__LAUNCHPAD_" not in code
    assert "GATEWAY_TOOLS = gateway_tools" in code
    assert "# <launchpad-gateway-mcp:v1>" in code
    target = tmp_path / "gateway_main.py"
    target.write_text(code, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)


def test_gateway_block_has_no_module_scope_imports(tmp_path: Path):
    """The documented v1 conversion blocker was an import-time crash. The client
    is fail-soft BY CONSTRUCTION: every risky import is function-local, so no
    module-scope statement in the block can raise."""
    import ast as _ast

    source = render_gateway_source(GATEWAY_SPEC)
    assert source
    tree = _ast.parse(source)
    for node in tree.body:
        assert not isinstance(node, (_ast.Import, _ast.ImportFrom)), _ast.dump(node)
        assert isinstance(node, (_ast.FunctionDef, _ast.Assign, _ast.AnnAssign)), (
            _ast.dump(node)
        )


def test_gateway_env_absent_means_no_tools_and_no_aws_call(gateway_module, monkeypatch):
    """Missing env is the ordinary case for a gateway agent whose bootstrap
    config has no gateway — it must degrade, not raise."""
    def explode(*_args, **_kwargs):
        raise AssertionError("no AWS call may be attempted without gateway env")

    monkeypatch.setattr(gateway_module, "_identity_client", explode)
    monkeypatch.setattr(gateway_module, "GATEWAY_URL", "")
    assert gateway_module.gateway_bearer_token() is None
    assert gateway_module.gateway_client() is None
    assert gateway_module.gateway_tools(ExitStack()) == []


def test_gateway_token_prefers_the_injected_workload_token(gateway_module, monkeypatch):
    monkeypatch.setattr(
        gateway_module.BedrockAgentCoreContext,
        "get_workload_access_token",
        staticmethod(lambda: "injected-token"),
    )
    monkeypatch.setattr(gateway_module, "WORKLOAD_NAME", "should-not-be-used")
    assert gateway_module.workload_identity_token() == "injected-token"


def test_gateway_token_falls_back_to_the_execution_role(gateway_module, monkeypatch):
    calls: list[str] = []

    class FakeIdentity:
        def get_workload_access_token(self, name):
            calls.append(name)
            return {"workloadAccessToken": "minted-token"}

    monkeypatch.setattr(
        gateway_module.BedrockAgentCoreContext,
        "get_workload_access_token",
        staticmethod(lambda: None),
    )
    monkeypatch.setattr(gateway_module, "WORKLOAD_NAME", "wl-runtime")
    monkeypatch.setattr(gateway_module, "_identity_client", lambda: FakeIdentity())
    assert gateway_module.workload_identity_token() == "minted-token"
    assert calls == ["wl-runtime"]


def test_gateway_token_without_either_mechanism_degrades(gateway_module, monkeypatch):
    monkeypatch.setattr(
        gateway_module.BedrockAgentCoreContext,
        "get_workload_access_token",
        staticmethod(lambda: None),
    )
    monkeypatch.setattr(gateway_module, "WORKLOAD_NAME", "")
    assert gateway_module.workload_identity_token() is None


def test_gateway_exchange_uses_m2m_and_degrades_on_failure(gateway_module, monkeypatch):
    captured: dict = {}

    class FakeDp:
        def get_resource_oauth2_token(self, **kwargs):
            captured.update(kwargs)
            return {"accessToken": "bearer-abc"}

    class FakeIdentity:
        dp_client = FakeDp()

    monkeypatch.setattr(gateway_module, "GATEWAY_URL", "https://gw.example/mcp")
    monkeypatch.setattr(gateway_module, "GATEWAY_PROVIDER", "launchpad-gw-m2m")
    monkeypatch.setattr(gateway_module, "GATEWAY_SCOPES", ["launchpad-gw/invoke"])
    monkeypatch.setattr(gateway_module, "workload_identity_token", lambda: "wit")
    monkeypatch.setattr(gateway_module, "_identity_client", lambda: FakeIdentity())

    assert gateway_module.gateway_bearer_token() == "bearer-abc"
    assert captured == {
        "workloadIdentityToken": "wit",
        "resourceCredentialProviderName": "launchpad-gw-m2m",
        "scopes": ["launchpad-gw/invoke"],
        "oauth2Flow": "M2M",
    }

    class Boom:
        dp_client = property(lambda self: (_ for _ in ()).throw(RuntimeError("denied")))

    monkeypatch.setattr(gateway_module, "_identity_client", lambda: Boom())
    assert gateway_module.gateway_bearer_token() is None


def test_gateway_session_start_failure_leaves_the_agent_usable(gateway_module, monkeypatch):
    stopped: list[str] = []

    class FailingClient:
        def start(self):
            raise RuntimeError("gateway unreachable")

        def stop(self, *_exc):
            stopped.append("stop")

    monkeypatch.setattr(gateway_module, "gateway_client", lambda: FailingClient())
    with ExitStack() as stack:
        assert gateway_module.gateway_tools(stack) == []
    # start() can fail partway through init, so it is torn down anyway
    assert stopped == ["stop"]
    # and the agent still builds with its own tools
    names = [t.tool_name for t in gateway_module.build_agent("a", "s").tools]
    assert names == ["calculator", "current_utc_time"]


def test_gateway_teardown_failure_cannot_fail_the_request(gateway_module, monkeypatch):
    """MCPClient re-raises its background task group's errors from __exit__, so
    handing the raw client to ExitStack.enter_context turns a *handled* mid-request
    failure into an unhandled one at unwind. Observed live: a tools/list 400 was
    logged and skipped, then httpx.HTTPStatusError escaped and 500'd the invoke."""
    class LateFailingClient:
        def start(self):
            return self

        def list_tools_sync(self):
            raise RuntimeError("400 Bad Request")

        def stop(self, *_exc):
            raise RuntimeError("unhandled errors in a TaskGroup")

    monkeypatch.setattr(gateway_module, "gateway_client", lambda: LateFailingClient())
    with ExitStack() as stack:                       # must NOT raise on unwind
        assert gateway_module.gateway_tools(stack) == []


def test_gateway_session_is_torn_down_after_a_successful_turn(gateway_module, monkeypatch):
    stopped: list[str] = []

    class OkClient:
        def start(self):
            return self

        def list_tools_sync(self):
            return []

        def stop(self, *_exc):
            stopped.append("stop")

    monkeypatch.setattr(gateway_module, "gateway_client", lambda: OkClient())
    with ExitStack() as stack:
        assert gateway_module.gateway_tools(stack) == []
        assert stopped == []          # still open while the agent may call a tool
    assert stopped == ["stop"]        # closed when the turn ends


def test_gateway_tools_are_appended_and_description_tunable(gateway_module, monkeypatch):
    class FakeMcpTool:
        def __init__(self, name):
            self.name = name
            self.description = "gateway default"

    class FakeGatewayTool:
        def __init__(self, name):
            self.mcp_tool = FakeMcpTool(name)
            self.tool_name = name

    tool_a = FakeGatewayTool("hr-database___get_employee")
    monkeypatch.setattr(
        gateway_module.BedrockAgentCoreContext,
        "get_config_bundle",
        staticmethod(lambda: {
            "tool_descriptions": {"hr-database___get_employee": "TREATMENT"}
        }),
    )
    agent = gateway_module.build_agent("a", "s", [tool_a])
    assert [t.tool_name for t in agent.tools] == [
        "calculator", "current_utc_time", "hr-database___get_employee",
    ]
    # An MCP tool's tool_spec is a property rebuilt on every access, so the
    # override has to land on mcp_tool.description or it is silently discarded.
    assert tool_a.mcp_tool.description == "TREATMENT"
