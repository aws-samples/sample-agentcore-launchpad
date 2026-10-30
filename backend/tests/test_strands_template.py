"""Template rendering, compilation, and the config-bundle fallback contract."""

import asyncio
import importlib.util
import os
import py_compile
import types
from contextlib import ExitStack
from pathlib import Path

import pytest
import strands

from app.schemas.agent import DEFAULT_MODEL_ID, AgentSpec, ToolRef
from app.services import opensearch_http as core
from app.templates.gateway_support import render_gateway_source
from app.templates.kb_support import mounted_kbs, render_direct_kb_source
from app.templates.strands_agent import base_requirements, render_main_py
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch


def _inject_bundle(module, monkeypatch, bundle):
    """Route the rendered module's bundle reader at a scripted bundle (N12 CA-2: the
    pointer comes from the request, the body from S3 — both stubbed here)."""
    monkeypatch.setattr(module, "_launchpad_bundle_ref",
                        lambda: "bndl_test:1" if bundle else None)
    monkeypatch.setattr(module, "_launchpad_fetch_bundle", lambda ref: dict(bundle))

SPEC = AgentSpec(
    name="tmpl-test-agent",
    method="zip_runtime",
    system_prompt="You are a template test agent. Be brief.",
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
    assert "_launchpad_bundle_ref" in code  # N12 CA-2 pointer reader
    assert "BedrockAgentCoreApp" in code
    assert "S3SessionManager" in code and "AgentCoreMemorySessionManager" not in code
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


def test_rendered_template_wires_the_global_model_credential():
    code = render_main_py(SPEC)
    assert 'MODEL_REGION = os.environ.get("LAUNCHPAD_MODEL_REGION", "us-west-2")' in code
    assert ('MODEL_CREDENTIAL = os.environ.get("LAUNCHPAD_MODEL_CREDENTIAL", '
            '"launchpad-global-bedrock")') in code
    assert "MODEL_MAX_TOKENS = 16384" in code  # N10-bug4: GLM default ceiling
    # spec C3: BedrockModel(api_key=…, region_name=…) with a ≥256 ceiling
    assert "api_key=_resolve_bedrock_api_key()" in code
    assert "region_name=MODEL_REGION" in code and "max_tokens=MODEL_MAX_TOKENS" in code
    # N12-bug11: the agent never sends a sampling field, and the inlined table lets the
    # shared builder drop one a model rejects (Kimi K3: temperature)
    assert "temperature=" not in code
    assert "MODEL_INFERENCE_PARAMS: dict[str, dict[str, bool]] = {" in code
    assert "'global.moonshotai.kimi-k3': {'temperature': False}," in code
    assert "BedrockModel(**_supported_model_config(config))" in code
    assert "__LAUNCHPAD_" not in code
    assert "mantle" not in code.lower() and "MODEL_SOURCE" not in code


def test_rendered_max_tokens_never_drops_below_the_glm_floor():
    from app.templates.strands_agent import rendered_max_tokens

    assert rendered_max_tokens(SPEC) == 16384  # N10-bug4: catalog default
    harness = SPEC.model_copy(update={"method": "harness", "max_tokens": 60})
    assert rendered_max_tokens(harness) == 256
    high = SPEC.model_copy(update={"method": "harness", "max_tokens": 9000})
    assert rendered_max_tokens(high) == 9000


def test_rendered_max_tokens_default_is_model_aware_and_explicit_values_win():
    # N10-bug4: both catalog models reason inside the per-call ceiling; 4096 was too
    # small once a skill's files are in context. Explicit values are untouched.
    from app.templates.strands_agent import rendered_max_tokens

    kimi = SPEC.model_copy(update={"model_id": "global.moonshotai.kimi-k3"})
    assert rendered_max_tokens(kimi) == 16384
    assert "MODEL_MAX_TOKENS = 16384" in render_main_py(kimi)
    explicit = SPEC.model_copy(update={"method": "harness", "max_tokens": 4096})
    assert rendered_max_tokens(explicit) == 4096
    assert "MODEL_MAX_TOKENS = 4096" in render_main_py(explicit)


class _FakeIdentity:
    def __init__(self):
        self.calls: list[dict] = []
        self.dp_client = self

    def get_resource_api_key(self, **kw):
        self.calls.append(kw)
        return {"apiKey": "bedrock-api-key"}

    def get_workload_access_token(self, name):
        self.calls.append({"self_mint": name})
        return {"workloadAccessToken": f"minted-for-{name}"}


def _wire_identity(template_module, monkeypatch, injected_token="wl-token"):
    identity = _FakeIdentity()
    monkeypatch.setattr(template_module, "_identity_client", lambda: identity)
    monkeypatch.setattr(
        template_module.BedrockAgentCoreContext, "get_workload_access_token",
        staticmethod(lambda: injected_token),
    )
    built: list[dict] = []

    class FakeBedrockModel:
        def __init__(self, **kwargs):
            built.append(dict(kwargs))

    monkeypatch.setattr(template_module, "BedrockModel", FakeBedrockModel)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    template_module._BEDROCK_API_KEY.clear()
    return identity, built


def test_build_model_exchanges_the_workload_token_once_and_exports_the_bearer(
    template_module, monkeypatch
):
    identity, built = _wire_identity(template_module, monkeypatch)
    template_module.build_model()
    template_module.build_model()
    assert identity.calls == [{
        "workloadIdentityToken": "wl-token",
        "resourceCredentialProviderName": "launchpad-global-bedrock",
    }]  # cached in-process after the first exchange
    assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "bedrock-api-key"
    assert built == [
        {"model_id": DEFAULT_MODEL_ID, "region_name": "us-west-2", "max_tokens": 16384},
    ] * 2
    assert type(template_module.build_agent("a", "s").model).__name__ == "FakeBedrockModel"


def test_build_model_self_mints_when_no_token_was_injected(template_module, monkeypatch):
    identity, _ = _wire_identity(template_module, monkeypatch, injected_token=None)
    monkeypatch.setenv("LAUNCHPAD_WORKLOAD_NAME", "tmpl_test_agent_ab12cd-0123456789")
    template_module.build_model()
    assert identity.calls[0] == {"self_mint": "tmpl_test_agent_ab12cd-0123456789"}
    expected_token = "minted-for-tmpl_test_agent_ab12cd-0123456789"
    assert identity.calls[1]["workloadIdentityToken"] == expected_token


def test_build_model_fails_loudly_without_any_workload_token(template_module, monkeypatch):
    _wire_identity(template_module, monkeypatch, injected_token=None)
    monkeypatch.delenv("LAUNCHPAD_WORKLOAD_NAME", raising=False)
    with pytest.raises(RuntimeError, match="runtimeUserId"):
        template_module.build_model()
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ


def test_agent_events_drop_reasoning_and_keep_text(template_module):
    """GLM 5.3 streams reasoningContent before its answer; the console gets text only."""

    class FakeAgent:
        async def stream_async(self, prompt):
            yield {"reasoning": True, "reasoningText": "let me think"}
            yield {"data": "Hello"}
            yield {"reasoningText": "more thinking", "data": "ignored-with-reasoning"}
            yield {"result": "Hello"}

    events = asyncio.run(_collect(template_module.agent_events(FakeAgent(), "hi")))
    assert events == [{"event": "delta", "text": "Hello"}, {"event": "complete", "result": "Hello"}]


async def _collect(iterator):
    return [event async for event in iterator]


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
    """Import the rendered template with a stubbed strands module.

    Pre-wired with a working fake Identity exchange and a stubbed ``BedrockModel``
    (see ``_wire_identity``) so every pre-existing test that calls ``build_agent``/
    ``build_model`` without caring about the credential exchange keeps working; a
    test that DOES care re-wires it explicitly (idempotent — monkeypatch allows a
    second ``setattr`` on the same target within one test).
    """
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    monkeypatch.setattr(strands, "tool", _FakeTool)

    target = tmp_path / "rendered_main.py"
    target.write_text(render_main_py(SPEC), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("rendered_main", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _wire_identity(module, monkeypatch)
    return module


def test_defaults_apply_without_bundle(template_module, monkeypatch):
    _inject_bundle(template_module, monkeypatch, {})
    assert template_module.resolve_system_prompt() == SPEC.system_prompt
    default_desc = template_module.DEFAULT_TOOL_DESCRIPTIONS["calculator"]
    assert template_module.resolve_tool_description("calculator") == default_desc


def test_bundle_overrides_prompt_and_tool_descriptions(template_module, monkeypatch):
    bundle = {
        "system_prompt": "OVERRIDDEN prompt from treatment bundle",
        "tool_descriptions": {"calculator": "OVERRIDDEN calculator description"},
    }
    _inject_bundle(template_module, monkeypatch, bundle)
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
    _inject_bundle(template_module, monkeypatch, bundle)
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
    _inject_bundle(module, monkeypatch, {})
    assert module.resolve_tool_description("calculator") == "promoted description"


def test_template_tools_work(template_module):
    assert template_module.calculator("2+2*3") == "8"
    assert template_module.current_utc_time().startswith("20")


def test_build_agent_omits_session_manager_without_memory(template_module, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_MEMORY_ID", "")
    agent = template_module.build_agent("agent__river", "session-one")
    assert not hasattr(agent, "session_manager")
    assert not hasattr(agent, "hooks")


KB_SPEC = AgentSpec(**{
    **SPEC.model_dump(),
    "knowledge_bases": [
        {"kb_id": "KB111", "name": "fund-docs", "description": "fund product PDFs"},
        {"kb_id": "KB222", "name": "faq", "description": ""},
    ],
})


def _import_rendered(spec, tmp_path: Path, monkeypatch, stem: str):
    """Same default identity pre-wiring as the ``template_module`` fixture, for the
    other spec-shaped fixtures (``kb_module``, ``toolkit_module``, ``gateway_module``)
    that build their own module instead of using ``SPEC`` directly."""
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    monkeypatch.setattr(strands, "tool", _FakeTool)
    target = tmp_path / f"{stem}.py"
    target.write_text(render_main_py(spec), encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location(stem, target)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    _wire_identity(module, monkeypatch)
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


class _RecordingOpenSearch(FakeOpenSearch):
    """The self-hosted KB substrate (N6): KB111's index holds one chunk, KB222's
    index always blows up, and every k-NN call is recorded as ``(index, k)``."""

    def __init__(self, embedder: FakeEmbedder):
        super().__init__()
        self.searched: list[tuple[str, int]] = []
        idx = core.kb_index_name("KB111", "")
        self.ensure_index(idx, core.kb_index_body())
        text = "Fund AUM was 1.2bn as of Aug 2021."
        self.put_doc(idx, "c0", {
            "kb_id": "KB111", "doc_uri": "s3://bucket/fund.pdf", "doc_name": "fund.pdf",
            "chunk_index": 0, "text": text, "embedding": embedder.embed(text), "metadata": {},
        })

    def knn_search(self, index, vector, k, filters, include_embedding=False):
        self.searched.append((index, k))
        if index == core.kb_index_name("KB222", ""):
            raise RuntimeError("index not ready")
        return super().knn_search(index, vector, k, filters, include_embedding)


def _wire_kb_substrate(kb_module, monkeypatch) -> _RecordingOpenSearch:
    embedder = FakeEmbedder()
    opensearch = _RecordingOpenSearch(embedder)
    monkeypatch.setattr(kb_module, "_kb_opensearch", lambda: opensearch)
    monkeypatch.setattr(kb_module, "_kb_embedder", lambda: embedder)
    return opensearch


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
    assert "one k-NN similarity search" in direct_source
    assert "multi-query" in direct_source
    assert "Slower than kb_search" in direct_source
    # the Bedrock KB data plane is gone (N6 R10) and nothing names a region or host
    for gone in ("bedrock-agent-runtime", "agentic_retrieve_stream", "AgenticRetrieveStream",
                 "us-west-2", "amazonaws"):
        assert gone not in direct_source, gone
    assert 'os.environ.get("LAUNCHPAD_KB_OS_ENDPOINT"' in direct_source
    assert 'os.environ.get("AWS_REGION", "cn-northwest-1")' in direct_source
    target = tmp_path / "launchpad_kb_tools.py"
    target.write_text(direct_source, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)


def test_kb_tools_registered_only_when_mounted(kb_module, template_module):
    assert kb_module.MOUNTED_KBS
    assert "one k-NN similarity search" in kb_module.kb_search.tool_spec["description"]
    assert "multi-query" in kb_module.kb_deep_search.tool_spec["description"]
    assert "Slower than kb_search" in kb_module.kb_deep_search.tool_spec["description"]
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
    opensearch = _wire_kb_substrate(kb_module, monkeypatch)
    out = kb_module.kb_search("what was the AUM?")
    # one k-NN query per mounted KB, KB_RESULTS wide, against that KB's own index
    assert opensearch.searched == [
        (core.kb_index_name("KB111", ""), kb_module.KB_RESULTS),
        (core.kb_index_name("KB222", ""), kb_module.KB_RESULTS),
    ]
    assert out.startswith("[fund-docs] 1 passage(s):")
    assert "Fund AUM was 1.2bn" in out
    assert "score=" in out and "source=s3://bucket/fund.pdf" in out
    # a broken KB degrades to a line, it does not raise
    assert "[faq] search failed: RuntimeError: index not ready" in out


def test_kb_search_targets_one_kb_and_rejects_unknown_ids(kb_module, monkeypatch):
    opensearch = _wire_kb_substrate(kb_module, monkeypatch)

    kb_module.kb_search("aum", "KB111")
    assert [index for index, _ in opensearch.searched] == [core.kb_index_name("KB111", "")]

    out = kb_module.kb_search("aum", "KB999")
    assert "not mounted" in out and "KB111, KB222" in out
    assert len(opensearch.searched) == 1  # no request issued for an unmounted id

    assert "non-empty query" in kb_module.kb_search("   ")


def test_kb_search_reports_empty_results(kb_module, monkeypatch):
    opensearch = _wire_kb_substrate(kb_module, monkeypatch)
    opensearch.indices[core.kb_index_name("KB111", "")].clear()
    assert kb_module.kb_search("aum", "KB111") == "[fund-docs] no matching passages."


def test_kb_search_folds_an_embedding_failure_per_kb(kb_module, monkeypatch):
    """A Titan/credential failure is a tool-text line too — never an exception."""
    _wire_kb_substrate(kb_module, monkeypatch)

    def _boom():
        raise RuntimeError("no workload access token")

    monkeypatch.setattr(kb_module, "_kb_embedder", _boom)
    out = kb_module.kb_search("aum")
    assert "[fund-docs] search failed: RuntimeError: no workload access token" in out
    assert "[faq] search failed: RuntimeError: no workload access token" in out


def test_kb_deep_search_is_a_wider_multi_query_knn(kb_module, monkeypatch):
    """Ruling R10: the question plus its clauses, 2× KB_RESULTS, de-duplicated by
    (doc_uri, chunk_index) — no FM planner, no AgenticRetrieveStream."""
    opensearch = _wire_kb_substrate(kb_module, monkeypatch)
    out = kb_module.kb_deep_search("compare the two strategies; which has more AUM?", "KB111")
    queries = 1 + len(kb_module._sub_queries("compare the two strategies; which has more AUM?"))
    assert queries == 3
    assert opensearch.searched == [(core.kb_index_name("KB111", ""), kb_module.KB_DEEP_RESULTS)] * 3
    assert kb_module.KB_DEEP_RESULTS == 2 * kb_module.KB_RESULTS
    # the one chunk every sub-query returned appears once
    assert out.startswith("[fund-docs] 1 passage(s):")
    assert out.count("source=s3://bucket/fund.pdf") == 1
    assert "Fund AUM was 1.2bn" in out


def test_sub_queries_split_on_clauses_and_drop_short_fragments(kb_module):
    assert kb_module._sub_queries("vacation days; carry-over rules, and expenses?") == [
        "vacation days", "carry-over rules", "expenses",
    ]
    assert kb_module._sub_queries("is it ok") == []  # the full question is already searched
    assert kb_module._sub_queries("a; b! c?") == []  # fragments under four characters


def test_kb_deep_search_fans_out_and_folds_failures_per_kb(kb_module, monkeypatch):
    opensearch = _wire_kb_substrate(kb_module, monkeypatch)
    out = kb_module.kb_deep_search("aum")
    assert {index for index, _ in opensearch.searched} == {
        core.kb_index_name("KB111", ""), core.kb_index_name("KB222", ""),
    }
    assert "[fund-docs] 1 passage(s):" in out
    assert "[faq] search failed: RuntimeError: index not ready" in out


def test_kb_deep_search_degrades_on_raise_and_bad_id(kb_module, monkeypatch):
    def _boom():
        raise RuntimeError("endpoint unreachable")

    monkeypatch.setattr(kb_module, "_kb_embedder", lambda: FakeEmbedder())
    monkeypatch.setattr(kb_module, "_kb_opensearch", _boom)
    assert kb_module.kb_deep_search("aum", "KB111") == (
        "[fund-docs] search failed: RuntimeError: endpoint unreachable"
    )

    opensearch = _wire_kb_substrate(kb_module, monkeypatch)
    out = kb_module.kb_deep_search("aum", "KB999")
    assert "not mounted" in out and "KB111, KB222" in out
    assert opensearch.searched == []  # no request issued for an unmounted id
    assert "non-empty query" in kb_module.kb_deep_search("   ")


def test_invoke_closes_batched_session_manager(template_module, monkeypatch):
    """Buffered memory writes must flush even if nothing else does: invoke enters
    the session manager as a context manager, so close() runs on the way out."""
    events: list[str] = []

    class FakeManager:
        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, *exc):
            events.append("exit")

    manager = FakeManager()
    monkeypatch.setattr(template_module, "memory_session_manager", lambda a, s: manager)

    captured = {}

    def fake_build_agent(actor_id, session_id, extra_tools=None, session_manager=None):
        captured["session_manager"] = session_manager

        async def stream_async(prompt):
            events.append("invoke")
            yield {"result": "ok"}

        return types.SimpleNamespace(stream_async=stream_async)

    monkeypatch.setattr(template_module, "build_agent", fake_build_agent)

    out = _drain(template_module.invoke({"prompt": "hi"}))

    assert out == [{"event": "complete", "result": "ok"}]
    assert captured["session_manager"] is manager
    assert events == ["enter", "invoke", "exit"]


def _drain(agen):
    async def collect():
        return [event async for event in agen]

    return asyncio.run(collect())


def test_invoke_streams_deltas_and_tool_calls_like_the_harness(template_module, monkeypatch):
    """The zip runtime's SSE envelope must match the container template's
    (delta/tool/complete) so Chat renders live tokens and tool calls for both."""
    strands_events = [
        {"event": {"messageStart": {"role": "assistant"}}},
        {"data": "Let me ", "delta": {"text": "Let me "}},
        {"data": "check.", "delta": {"text": "check."}},
        # Tool use: the raw contentBlockStart, then per-input-delta current_tool_use
        {"event": {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": "t1", "name": "calculator"}}}}},
        {"current_tool_use": {"toolUseId": "t1", "name": "calculator", "input": "{"}},
        {"current_tool_use": {"toolUseId": "t1", "name": "calculator", "input": '{"e'}},
        {"data": "8", "delta": {"text": "8"}},
        {"result": "Let me check.8"},
    ]

    def fake_build_agent(actor_id, session_id, extra_tools=None, session_manager=None):
        async def stream_async(prompt):
            assert prompt == "2+2*3"
            for event in strands_events:
                yield event

        return types.SimpleNamespace(stream_async=stream_async)

    monkeypatch.setattr(template_module, "memory_session_manager", lambda a, s: None)
    monkeypatch.setattr(template_module, "build_agent", fake_build_agent)

    out = _drain(template_module.invoke({"prompt": "2+2*3"}))

    assert out == [
        {"event": "delta", "text": "Let me "},
        {"event": "delta", "text": "check."},
        {"event": "tool", "name": "calculator", "id": "t1"},  # emitted once per tool use
        {"event": "delta", "text": "8"},
        {"event": "complete", "result": "Let me check.8"},
    ]


def test_invoke_rejects_empty_prompt_as_error_event(template_module):
    assert _drain(template_module.invoke({"prompt": "  "})) == [
        {"event": "error", "message": "payload must include a non-empty 'prompt'"}
    ]


def test_invoke_emits_heartbeat_during_a_quiet_tool_call(template_module, monkeypatch):
    def fake_build_agent(actor_id, session_id, extra_tools=None, session_manager=None):
        async def stream_async(prompt):
            await asyncio.sleep(0.05)  # a slow tool
            yield {"result": "done"}

        return types.SimpleNamespace(stream_async=stream_async)

    monkeypatch.setattr(template_module, "memory_session_manager", lambda a, s: None)
    monkeypatch.setattr(template_module, "build_agent", fake_build_agent)
    monkeypatch.setattr(template_module, "SSE_HEARTBEAT_INTERVAL_S", 0.01)

    out = _drain(template_module.invoke({"prompt": "hi"}))

    assert out[-1] == {"event": "complete", "result": "done"}
    assert out[0]["event"] == "heartbeat" and "timestamp" in out[0]


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
    toolkit's provenance comment legitimately names what was dropped. Every
    template now imports ``BedrockModel`` at module scope for the Global Bedrock
    credential exchange (spec C3/C4), so this no longer asserts its absence.
    """
    assert toolkit_module.MODEL_ID == DEFAULT_MODEL_ID
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
    _inject_bundle(toolkit_module, monkeypatch,
                   {"tool_descriptions": {"get_pay_stub": "TREATMENT DESC"}})
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


def test_gateway_block_module_scope_is_limited_to_sigv4_deps_and_definitions(tmp_path: Path):
    """The documented v1 conversion blocker was an import-time crash from a
    risky, could-be-missing import. ``_LaunchpadSigV4Auth(httpx.Auth)`` is a
    class statement that resolves at module **import** time, so the SigV4
    transport's own imports (httpx, botocore, mcp) cannot be function-local the
    way the rest of this fail-soft block is — but they are guaranteed present
    (declared dependencies of strands-agents / bedrock_agentcore), so this
    trades away no real fail-softness. Nothing else may appear at module scope."""
    import ast as _ast

    source = render_gateway_source(GATEWAY_SPEC)
    assert source
    tree = _ast.parse(source)
    imported_names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            imported_names.update(alias.name for alias in node.names)
            continue
        assert isinstance(
            node, (_ast.ClassDef, _ast.FunctionDef, _ast.Assign, _ast.AnnAssign)
        ), _ast.dump(node)
    assert imported_names == {"httpx", "SigV4Auth", "AWSRequest", "streamablehttp_client"}


def test_sigv4_transport_signs_each_request_for_the_gateway_region():
    """The same shared transport ``harness_convert`` grafts into a converted
    twin's mcp_client/client.py: signs each Gateway MCP request with the
    runtime's own execution role, not a minted bearer token."""
    import httpx
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    from app.templates.gateway_support import render_sigv4_transport

    # the template carries the stream-A3 principal placeholder; consumers render it
    source = render_sigv4_transport()

    class Frozen:
        access_key, secret_key, token = "AKIDEXAMPLE", "secret", "session-token"

    class Session:
        def get_credentials(self):
            return type("C", (), {"get_frozen_credentials": lambda self: Frozen()})()

    seen: dict = {}
    scope = {
        "httpx": httpx, "boto3": type("B", (), {"Session": Session}),
        "SigV4Auth": SigV4Auth, "AWSRequest": AWSRequest,
        "streamablehttp_client": lambda url, auth: seen.update(url=url, auth=auth),
    }
    exec(source, scope)  # noqa: S102
    scope["_launchpad_sigv4_streamablehttp"]("https://gw.example/mcp", "us-east-1")
    request = httpx.Request(
        "POST", "https://gw.example/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Connection": "keep-alive"},
    )
    flow = seen["auth"].auth_flow(request)
    signed = next(flow)
    auth = signed.headers["authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock-agentcore/aws4_request" in auth
    assert "connection" not in auth.split("SignedHeaders=")[1].split(",")[0]
    assert signed.headers["x-amz-security-token"] == "session-token"
    assert "x-amz-date" in signed.headers


def test_gateway_env_absent_means_no_tools_and_no_aws_call(gateway_module, monkeypatch):
    """Missing env is the ordinary case for a gateway agent whose bootstrap
    config has no gateway — it must degrade, not raise, and must not touch AWS."""
    def explode(*_args, **_kwargs):
        raise AssertionError("no AWS call may be attempted without gateway env")

    monkeypatch.setattr(gateway_module.boto3, "Session", explode)
    monkeypatch.setattr(gateway_module, "GATEWAY_URL", "")
    assert gateway_module.gateway_client() is None
    assert gateway_module.gateway_tools(ExitStack()) == []


def test_gateway_clients_load_each_target_and_close_both_sessions(gateway_module, monkeypatch):
    from types import SimpleNamespace

    loaded, stopped = [], []

    class Client:
        def __init__(self, url):
            self.url = url

        def start(self):
            loaded.append(self.url)

        def stop(self, *_args):
            stopped.append(self.url)

        def list_tools_sync(self):
            return [SimpleNamespace(tool_name=self.url), SimpleNamespace(tool_name="unselected")]

    monkeypatch.setattr(gateway_module, "gateway_client", Client)
    monkeypatch.setattr(gateway_module, "GATEWAY_CONFIG",
                        '[{"url":"gateway-a","tools":["gateway-a"]},'
                        '{"url":"gateway-b","tools":["gateway-b"]}]')
    with ExitStack() as stack:
        tools = gateway_module.gateway_tools(stack)
        assert [tool.tool_name for tool in tools] == ["gateway-a", "gateway-b"]
        assert loaded == ["gateway-a", "gateway-b"] and stopped == []
    assert stopped == ["gateway-b", "gateway-a"]


def test_gateway_client_wires_the_sigv4_transport_with_url_and_region(
    gateway_module, monkeypatch,
):
    """``gateway_client()`` signs with the runtime's own execution role, not a
    minted bearer — it never asks AgentCore Identity for anything."""
    captured: dict = {}

    def fake_transport(url, region):
        captured["url"] = url
        captured["region"] = region
        return "sigv4-ctx-manager"

    monkeypatch.setattr(gateway_module, "_launchpad_sigv4_streamablehttp", fake_transport)
    monkeypatch.setattr(gateway_module, "GATEWAY_URL", "https://gw.example/mcp")
    monkeypatch.setattr(gateway_module, "GATEWAY_REGION", "cn-northwest-1")
    client = gateway_module.gateway_client()
    assert client is not None
    # MCPClient stores the factory lazily; invoking it directly proves the wiring
    # without opening a real network session.
    assert client._transport_callable() == "sigv4-ctx-manager"
    assert captured == {"url": "https://gw.example/mcp", "region": "cn-northwest-1"}


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
    _inject_bundle(gateway_module, monkeypatch, {
            "tool_descriptions": {"hr-database___get_employee": "TREATMENT"}
        })
    agent = gateway_module.build_agent("a", "s", [tool_a])
    assert [t.tool_name for t in agent.tools] == [
        "calculator", "current_utc_time", "hr-database___get_employee",
    ]
    # An MCP tool's tool_spec is a property rebuilt on every access, so the
    # override has to land on mcp_tool.description or it is silently discarded.
    assert tool_a.mcp_tool.description == "TREATMENT"


def test_gateway_tools_are_filtered_to_the_selected_records_targets(gateway_module, monkeypatch):
    """launchpad-gw carries every Registry record's target; the client loads only the
    patterns the deployer injected (LAUNCHPAD_GATEWAY_TOOLS), never the other targets."""
    class Tool:
        def __init__(self, name):
            self.tool_name = name

    class OkClient:
        def start(self):
            return self

        def list_tools_sync(self):
            return [Tool("hr-database___get_employee"), Tool("hr-database___create_payout"),
                    Tool("office-facts___get_office_fact")]

        def stop(self, *_exc):
            pass

    monkeypatch.setattr(gateway_module, "gateway_client", lambda: OkClient())
    monkeypatch.setattr(gateway_module, "GATEWAY_TOOL_PATTERNS",
                        ["hr-database___get_employee"])
    with ExitStack() as stack:
        names = [t.tool_name for t in gateway_module.gateway_tools(stack)]
    assert names == ["hr-database___get_employee"]
    monkeypatch.setattr(gateway_module, "GATEWAY_TOOL_PATTERNS", ["hr-database___*"])
    with ExitStack() as stack:
        names = [t.tool_name for t in gateway_module.gateway_tools(stack)]
    assert names == ["hr-database___get_employee", "hr-database___create_payout"]


# ─── N10v3-bug5 live correction: self-mint is a last resort, tried once ───────
class _LinkedRefusal(Exception):
    """botocore-shaped ValidationException Identity answers for a runtime-linked identity."""

    response = {"Error": {
        "Code": "ValidationException",
        "Message": "WorkloadIdentity is linked to a service and cannot retrieve an access "
                   "token by the caller",
    }}


def test_self_mint_refusal_is_one_readable_error_and_disables_the_branch(
    template_module, monkeypatch,
):
    identity, _ = _wire_identity(template_module, monkeypatch, injected_token=None)
    monkeypatch.setenv("LAUNCHPAD_WORKLOAD_NAME", "rt-linked-0123456789")
    template_module._SELF_MINT.clear()

    def refuse(name):
        identity.calls.append({"self_mint": name})
        raise _LinkedRefusal(_LinkedRefusal.response["Error"]["Message"])

    monkeypatch.setattr(identity, "get_workload_access_token", refuse)
    for _ in range(5):  # five judge/tool calls → ONE Identity call, five readable errors
        with pytest.raises(RuntimeError) as excinfo:
            template_module.build_model()
        message = str(excinfo.value)
        assert "linked to the AgentCore Runtime" in message and "cannot self-mint" in message
        assert "caller must send runtimeUserId" in message
        assert "rt-linked" not in message  # no identifiers beyond the cause
    assert identity.calls == [{"self_mint": "rt-linked-0123456789"}]
    assert template_module._SELF_MINT == {"disabled": "linked"}
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ
    # an unrelated Identity failure is not swallowed into the disabled state
    template_module._SELF_MINT.clear()

    def throttle(name):
        raise RuntimeError("ThrottlingException: slow down")

    monkeypatch.setattr(identity, "get_workload_access_token", throttle)
    with pytest.raises(RuntimeError, match="ThrottlingException"):
        template_module.build_model()
    assert template_module._SELF_MINT == {}


def test_prefetch_captures_the_injected_token_and_caches_the_key_for_other_threads(
    template_module, monkeypatch,
):
    """N12-bug8: the request-thread hook copies the injected token into the process slot
    and resolves the key once; a bare thread (empty context) then needs neither."""
    import threading

    identity, _ = _wire_identity(template_module, monkeypatch, injected_token="wl-token")
    template_module._WORKLOAD_TOKEN.clear()
    assert template_module._prefetch_bedrock_api_key() == "bedrock-api-key"
    assert template_module._WORKLOAD_TOKEN == {"value": "wl-token"}
    monkeypatch.setattr(template_module.BedrockAgentCoreContext, "get_workload_access_token",
                        staticmethod(lambda: None))  # the pool thread sees no context
    seen: dict[str, str] = {}
    thread = threading.Thread(target=lambda: seen.update(
        key=template_module._resolve_bedrock_api_key()))
    thread.start()
    thread.join(5)
    assert seen == {"key": "bedrock-api-key"}
    assert len(identity.calls) == 1  # the cache answered the thread
    template_module._WORKLOAD_TOKEN.clear()


def test_the_no_token_error_names_runtime_user_id_and_the_self_mint_limit(
    template_module, monkeypatch,
):
    _wire_identity(template_module, monkeypatch, injected_token=None)
    monkeypatch.delenv("LAUNCHPAD_WORKLOAD_NAME", raising=False)
    template_module._SELF_MINT.clear()
    with pytest.raises(RuntimeError) as excinfo:
        template_module.build_model()
    assert "must carry runtimeUserId" in str(excinfo.value)
    assert "runtime-linked workload identity cannot self-mint" in str(excinfo.value)


# ─── N-R6: provider table + dispatch in the shared model block ──────────────────────

SF_ID = "siliconflow.zai-org/GLM-5.3"
SF_SPEC = SPEC.model_copy(update={"model_id": SF_ID})


@pytest.mark.siliconflow
def test_rendered_template_inlines_the_provider_table_and_dispatches_on_it():
    code = render_main_py(SF_SPEC)
    assert f'MODEL_ID = "{SF_ID}"' in code
    assert "MODEL_PROVIDERS: dict[str, dict[str, Any]] = {" in code
    assert ("'siliconflow.zai-org/GLM-5.3': {'kind': 'openai_compatible', "
            "'base_url': 'https://api.siliconflow.cn/v1', 'api_model_name': 'zai-org/GLM-5.3', "
            "'credential_provider': 'launchpad-siliconflow', 'streaming': True},") in code
    assert "'global.zai.glm-5.3': {'kind': 'bedrock', 'base_url': None," in code
    assert "def _launchpad_chat_model(" in code and "def _launchpad_openai_model(" in code
    assert "from strands.models.openai import OpenAIModel" in code
    assert "return _launchpad_chat_model(model_id=MODEL_ID, max_tokens=MODEL_MAX_TOKENS)" in code
    assert "MODEL_MAX_TOKENS = 8192" in code  # OpenAI-compatible default ceiling
    assert "__LAUNCHPAD_" not in code
    # the Harness model/load.py slice carries the same table and dispatch
    from app.templates.model_support import render_model_load

    load = render_model_load(SF_SPEC)
    assert "MODEL_PROVIDERS" in load and "_launchpad_chat_model" in load and "OpenAIModel" in load


@pytest.mark.siliconflow
def test_rendered_siliconflow_runtime_builds_an_openai_model_with_the_providers_key(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    monkeypatch.setattr(strands, "tool", _FakeTool)
    target = tmp_path / "rendered_sf.py"
    target.write_text(render_main_py(SF_SPEC), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("rendered_sf", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity, built = _wire_identity(module, monkeypatch)

    class FakeIdentitySF(_FakeIdentity):
        def get_resource_api_key(self, **kw):
            self.calls.append(kw)
            return {"apiKey": f"key-for-{kw['resourceCredentialProviderName']}"}

    identity = FakeIdentitySF()
    monkeypatch.setattr(module, "_identity_client", lambda: identity)
    openai_built: list[dict] = []

    class FakeOpenAIModel:
        def __init__(self, **kwargs):
            openai_built.append(kwargs)

    monkeypatch.setattr(module, "OpenAIModel", FakeOpenAIModel)
    module._PROVIDER_API_KEYS.clear()

    module.build_model()
    module.build_model()
    # exactly one exchange, at the SiliconFlow credential provider — never the Bedrock one
    assert identity.calls == [{"workloadIdentityToken": "wl-token",
                               "resourceCredentialProviderName": "launchpad-siliconflow"}]
    assert built == []  # no BedrockModel
    assert openai_built == [{
        "client_args": {"base_url": "https://api.siliconflow.cn/v1",
                        "api_key": "key-for-launchpad-siliconflow"},
        "model_id": "zai-org/GLM-5.3",
        "params": {"max_tokens": 8192},
        "stream": True,
    }] * 2
    # the key stays a client argument: nothing exported to the environment
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ
    assert not any("key-for" in v for v in os.environ.values())
    # a Bedrock id through the same dispatch still takes the Bedrock path
    module._launchpad_chat_model(model_id=DEFAULT_MODEL_ID, max_tokens=512)
    assert built == [{"model_id": DEFAULT_MODEL_ID, "region_name": "us-west-2",
                      "max_tokens": 512}]
    assert identity.calls[-1]["resourceCredentialProviderName"] == "launchpad-global-bedrock"


def test_platform_requirements_carry_the_openai_extra_everywhere():
    from app.templates.eval_worker import base_requirements as worker_requirements
    from app.templates.strands_a2a_agent import a2a_base_requirements

    assert "strands-agents[otel,openai]==1.47.0" in base_requirements()
    assert "strands-agents[a2a,otel,openai]==1.47.0" in a2a_base_requirements()
    assert "strands-agents[otel,openai]==1.47.0" in worker_requirements()
    harness = (Path(__file__).resolve().parents[1] / "app" / "templates" / "harness_runtime"
               / "requirements.txt.tmpl").read_text(encoding="utf-8")
    assert "strands-agents[otel,openai]==1.47.0" in harness
