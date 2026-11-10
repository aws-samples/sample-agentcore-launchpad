"""Studio artifact path: adapter wrap, bundle shim, API acceptance, deferred catalog model."""

import asyncio
import os
import py_compile
import sys
import types

import pytest

import app.routers.agents as agents_router
from app.schemas.agent import AgentSpec
from app.templates import model_support
from app.templates.studio_agent import StudioAdaptError, adapt_studio_code

SAMPLE_STUDIO_CODE = '''
import asyncio
from strands import Agent
from strands.models import BedrockModel

async def main():
    model = BedrockModel(model_id="global.zai.glm-5.3")
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, model=model,
        system_prompt="You are a helpful research assistant.",
    )
    response = agent("hello")
    print(response)

if __name__ == "__main__":
    asyncio.run(main())
'''

SF = "siliconflow.zai-org/GLM-5.3"
# What the canvas emits for two nodes (lib/code-generator.ts): the region line on every
# Bedrock model, the caching toggle regardless of the node's model.
CANVAS_CODE = (
    "import os\n"
    "from strands import Agent\n"
    "from strands.models import BedrockModel, CacheConfig\n\n"
    'researcher_model = BedrockModel(\n    model_id="global.moonshotai.kimi-k3",\n'
    '    region_name=os.environ.get("LAUNCHPAD_MODEL_REGION", "us-west-2"),\n'
    "    max_tokens=4096, temperature=0.3,\n)\n"
    f'writer_model = BedrockModel(\n    model_id="{SF}",\n'
    '    region_name=os.environ.get("LAUNCHPAD_MODEL_REGION", "us-west-2"),\n'
    '    max_tokens=512, temperature=0.2, cache_config=CacheConfig(strategy="auto"),\n)\n\n'
    "async def main(user_input, messages):\n    print(user_input)\n"
)


def test_adapt_studio_code_wraps_for_agentcore(tmp_path):
    adapted = adapt_studio_code(SAMPLE_STUDIO_CODE)
    assert "BedrockAgentCoreApp" in adapted
    assert "@app.entrypoint" in adapted
    # platform config-bundle shim injected
    assert "launchpad_config_bundle" in adapted or "get_config_bundle" in adapted
    target = tmp_path / "adapted.py"
    target.write_text(adapted, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)


def test_platform_accepts_studio_artifact(client, monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    res = client.post(
        "/api/agents",
        json={
            "name": "studio-sample-agent",
            "method": "studio",
            "system_prompt": "Strands Studio generated agent",
            "code": SAMPLE_STUDIO_CODE,
        },
    )
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["agent"]["method"] == "studio"
    assert launched == [body["job_id"]]
    spec = AgentSpec(**body["agent"]["spec"])
    assert spec.code and "strands" in spec.code


def test_generate_stage_uses_studio_code():
    from app.deployer.zip_runtime import _generate_code

    spec = AgentSpec(
        name="studio-x", method="studio", system_prompt="s", code=SAMPLE_STUDIO_CODE,
        model_credential="team-key", model_region="us-east-1",
    )
    code, source = _generate_code(spec)
    assert source == "studio artifact (adapted)"
    assert "BedrockAgentCoreApp" in code
    # the spec's constants reach the rendered model block (N-R6-obs2)
    assert 'MODEL_CREDENTIAL = os.environ.get("LAUNCHPAD_MODEL_CREDENTIAL", "team-key")' in code
    assert 'MODEL_REGION = os.environ.get("LAUNCHPAD_MODEL_REGION", "us-east-1")' in code

    template_spec = AgentSpec(name="tmpl-x", method="zip_runtime", system_prompt="s")
    _, template_source = _generate_code(template_spec)
    assert template_source == "strands template"


# ─── N-R6-obs2: the canvas model goes through the shared provider dispatch ───────────────


@pytest.mark.siliconflow
def test_adapt_grafts_the_import_and_renders_the_shared_model_block():
    adapted = adapt_studio_code(SAMPLE_STUDIO_CODE)
    assert "from strands.models import BedrockModel as _LaunchpadStockBedrockModel" in adapted
    assert "BedrockModel = _LaunchpadStudioModel" in adapted
    # the SAME block every runtime runs, with its two inlined catalog tables
    for needle in (
        "def _launchpad_chat_model(", "def _launchpad_openai_model(",
        "def _launchpad_bedrock_model(", "def _resolve_api_key(", "def _supported_model_config(",
        "MODEL_PROVIDERS: dict[str, dict[str, Any]] = {",
        "MODEL_INFERENCE_PARAMS: dict[str, dict[str, bool]] = {",
        "    'global.moonshotai.kimi-k3': {'temperature': False},",
        f"    {SF!r}: {{'kind': 'openai_compatible', 'base_url': 'https://api.siliconflow.cn/v1',",
        # the one construction site points at the aliased stock class, never at the alias
        "return _LaunchpadStockBedrockModel(**_supported_model_config(config))",
        "def launchpad_resolve_bedrock_api_key",  # studio-author opt-in kept
    ):
        assert needle in adapted, needle
    # no Bedrock-only construction is left
    assert "return BedrockModel(**_supported_model_config(config))" not in adapted
    assert "_launchpad_bedrock_model_class" not in adapted
    assert "_LaunchpadLazyBotoSession" not in adapted
    # the prelude precedes the first model construction and the module still compiles
    assert adapted.index("BedrockModel = _LaunchpadStudioModel") < adapted.index(
        'model = BedrockModel(model_id="global.zai.glm-5.3")'  # the canvas's own line
    )
    compile(adapted, "studio_runtime.py", "exec")
    assert adapt_studio_code(adapted) == adapted  # idempotent


def test_studio_model_block_is_the_strands_templates_block():
    """Byte-for-byte the sliced block (one substitution: the stock construction site)."""
    spec = AgentSpec(name="studio-x", method="studio", system_prompt="s", code=SAMPLE_STUDIO_CODE)
    adapted = adapt_studio_code(SAMPLE_STUDIO_CODE, spec)
    block = model_support.render_model_source(spec).replace(
        "return BedrockModel(**_supported_model_config(config))",
        "return _LaunchpadStockBedrockModel(**_supported_model_config(config))",
    )
    for chunk in block.split("\n\n\n"):
        assert chunk.strip() in adapted


def test_adapt_refuses_a_bedrock_model_without_the_anchored_import():
    with pytest.raises(StudioAdaptError, match="from strands.models import BedrockModel"):
        adapt_studio_code("import strands\nmodel = strands.models.BedrockModel(model_id='x')\n"
                          "async def main(u, m):\n    print(u)\n")


def test_adapt_without_any_bedrock_model_adds_no_prelude():
    """An OpenAI-only canvas never touches the catalog block (R16)."""
    adapted = adapt_studio_code("async def main(user_input, messages):\n    print(user_input)\n")
    assert "_launchpad_chat_model" not in adapted
    assert "_LaunchpadStockBedrockModel" not in adapted
    assert "launchpad_config_bundle" in adapted  # the bundle shim is unconditional
    compile(adapted, "x.py", "exec")


def test_prelude_lands_right_after_the_anchored_import_so_module_scope_models_work():
    """The canvas builds `<label>_model = BedrockModel(...)` at module scope, right after
    its imports; the alias must already be bound there (R16)."""
    lines = adapt_studio_code(CANVAS_CODE).splitlines()
    anchor_import = (
        "from strands.models import BedrockModel as _LaunchpadStockBedrockModel, CacheConfig"
    )
    anchor = lines.index(anchor_import)
    alias = lines.index("BedrockModel = _LaunchpadStudioModel")
    use = next(
        i for i, ln in enumerate(lines) if ln.startswith("researcher_model = BedrockModel(")
    )
    assert anchor < alias < use


class _StockBedrockModel:
    """Stand-in for strands' BedrockModel: records its kwargs, streams one frame."""

    def __init__(self, **config):
        self.config = dict(config)

    def get_config(self):
        return self.config

    def update_config(self, **kw):
        self.config.update(kw)

    async def stream(self, *args, **kwargs):
        yield {"provider": "bedrock"}

    async def structured_output(self, *args, **kwargs):
        yield {}


class _FakeOpenAIModel(_StockBedrockModel):
    """Stand-in for strands.models.openai.OpenAIModel (constructor kwargs recorded)."""

    async def stream(self, *args, **kwargs):
        yield {"provider": "openai_compatible"}


class _FakeIdentity:
    def __init__(self):
        self.calls = []
        self.dp_client = self

    def get_resource_api_key(self, **kw):
        self.calls.append(kw)
        return {"apiKey": "key-for-" + kw["resourceCredentialProviderName"]}


def _first(agen):
    async def go():
        async for event in agen:
            return event
    return asyncio.run(go())


def _exec_adapted(monkeypatch, code: str = SAMPLE_STUDIO_CODE) -> dict:
    # Warm the real package first: `strands/__init__.py` eagerly resolves
    # `strands.event_loop.streaming`'s `from ..models.model import Model`, which fails if
    # `sys.modules["strands.models"]` is already the fake below when that first happens.
    # Once cached, strands' own internal references are immune to the swap that follows —
    # only the adapted code's own `from strands.models import …` sees the fakes.
    import strands  # noqa: F401
    import strands.models.openai  # noqa: F401  (cached so the swap below is the only path)

    fake = types.ModuleType("strands.models")
    fake.BedrockModel = _StockBedrockModel
    fake.CacheConfig = lambda **kw: ("cache", kw)
    fake_openai = types.ModuleType("strands.models.openai")
    fake_openai.OpenAIModel = _FakeOpenAIModel
    monkeypatch.setitem(sys.modules, "strands.models", fake)
    monkeypatch.setitem(sys.modules, "strands.models.openai", fake_openai)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.delenv("LAUNCHPAD_WORKLOAD_NAME", raising=False)
    namespace: dict = {}
    exec(compile(adapt_studio_code(code), "studio.py", "exec"), namespace)
    return namespace


def test_deferred_model_is_a_strands_model_built_on_first_use(monkeypatch):
    from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
    from strands.models.model import Model

    monkeypatch.setenv("LAUNCHPAD_MODEL_CREDENTIAL", "team-key")
    namespace = _exec_adapted(monkeypatch, CANVAS_CODE)
    identity = _FakeIdentity()
    namespace["_identity_client"] = lambda: identity

    model = namespace["researcher_model"]
    # import-time construction (the canvas builds its models at module scope) touched no
    # token and no network; the Agent-facing surface answers from the pending kwargs
    assert isinstance(model, Model) and model.stateful is False
    assert identity.calls == [] and "AWS_BEARER_TOKEN_BEDROCK" not in os.environ
    assert model.config["model_id"] == "global.moonshotai.kimi-k3"
    assert model.get_config()["max_tokens"] == 4096
    model.update_config(max_tokens=2048)

    BedrockAgentCoreContext.set_workload_access_token("wl-token")
    assert _first(model.stream([])) == {"provider": "bedrock"}  # first real use
    assert identity.calls == [{"workloadIdentityToken": "wl-token",
                               "resourceCredentialProviderName": "team-key"}]
    assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "key-for-team-key"
    inner = model._lp_inner
    assert isinstance(inner, _StockBedrockModel)
    # the shared dispatch: MODEL_REGION, the updated ceiling, and — N12-bug11 — no
    # `temperature` for Kimi K3 (MODEL_INFERENCE_PARAMS says it rejects the field)
    assert inner.config == {"model_id": "global.moonshotai.kimi-k3",
                            "region_name": "us-west-2", "max_tokens": 2048}
    assert model.config is inner.config  # the built model's config is the one exposed


@pytest.mark.siliconflow
def test_deferred_openai_compatible_model_uses_the_provider_row_and_its_own_key(monkeypatch):
    from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

    namespace = _exec_adapted(monkeypatch, CANVAS_CODE)
    identity = _FakeIdentity()
    namespace["_identity_client"] = lambda: identity
    BedrockAgentCoreContext.set_workload_access_token("wl-token")

    model = namespace["writer_model"]
    assert _first(model.stream([])) == {"provider": "openai_compatible"}
    inner = model._lp_inner
    assert isinstance(inner, _FakeOpenAIModel)
    # base_url + api_model_name from MODEL_PROVIDERS, the key from the provider's OWN
    # Identity credential provider; the canvas's region line and caching kwarg never reach
    # the chat-completions body; `temperature` is kept (the SiliconFlow row accepts it)
    assert inner.config == {
        "client_args": {"base_url": "https://api.siliconflow.cn/v1",
                        "api_key": "key-for-launchpad-siliconflow"},
        "model_id": "zai-org/GLM-5.3",
        "params": {"max_tokens": 512, "temperature": 0.2},
        "stream": True,
    }
    assert identity.calls == [{"workloadIdentityToken": "wl-token",
                               "resourceCredentialProviderName": "launchpad-siliconflow"}]
    assert "AWS_BEARER_TOKEN_BEDROCK" not in os.environ  # the Bedrock key was never needed


def test_deferred_model_without_a_token_fails_at_first_use_not_at_import(monkeypatch):
    from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

    namespace = _exec_adapted(monkeypatch)
    namespace["_identity_client"] = lambda: _FakeIdentity()
    BedrockAgentCoreContext.set_workload_access_token("")
    model = namespace["BedrockModel"](model_id="global.zai.glm-5.3")
    with pytest.raises(RuntimeError, match="runtimeUserId"):
        _first(model.stream([]))


# ── r8-nits (review follow-ups on N-R6-obs2): lock, count_tokens, warning, eager token ──


class _CountingStockModel(_StockBedrockModel):
    """Records how many instances were built and how `count_tokens` was called."""

    built = 0
    token_calls: list = []

    def __init__(self, **config):
        super().__init__(**config)
        type(self).built += 1

    async def count_tokens(self, *args, **kwargs):
        type(self).token_calls.append((args, kwargs))
        return 42


def test_deferred_model_first_use_is_built_once_under_a_lock(monkeypatch):
    """(a) Several threads hitting a never-built model at once (one Agent shared by
    concurrent requests) must construct the inner model exactly once — the old
    check-then-set let every racer build and exchange a key of its own."""
    import threading
    import time

    from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

    namespace = _exec_adapted(monkeypatch, CANVAS_CODE)
    namespace["_identity_client"] = lambda: _FakeIdentity()
    BedrockAgentCoreContext.set_workload_access_token("wl-token")
    # the SDK token is context-scoped — worker threads only find it through the process
    # slot the request thread fills (what the eager capture in `invoke()` is for)
    namespace["_capture_workload_token"]()
    model = namespace["researcher_model"]
    assert isinstance(model._lp_lock, type(threading.Lock()))

    built: list[object] = []
    real_build = namespace["_launchpad_chat_model"]

    def slow_build(**kwargs):
        time.sleep(0.05)  # widen the race window
        inner = real_build(**kwargs)
        built.append(inner)
        return inner

    namespace["_launchpad_chat_model"] = slow_build
    seen: list[object] = []
    threads = [threading.Thread(target=lambda: seen.append(model._lp_model())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1 and len(seen) == 8 and all(s is built[0] for s in seen)


def test_deferred_model_forwards_count_tokens_once_built(monkeypatch):
    """(b) strands' context manager calls `Model.count_tokens`; the deferred model
    forwards it to the inner model (building it on the way, like `stream` does)."""
    from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

    _CountingStockModel.built = 0
    _CountingStockModel.token_calls = []
    namespace = _exec_adapted(monkeypatch, CANVAS_CODE)
    sys.modules["strands.models"].BedrockModel = _CountingStockModel
    namespace["_LaunchpadStockBedrockModel"] = _CountingStockModel
    namespace["_identity_client"] = lambda: _FakeIdentity()
    BedrockAgentCoreContext.set_workload_access_token("wl-token")
    model = namespace["researcher_model"]
    assert asyncio.run(model.count_tokens([{"role": "user", "content": []}], None)) == 42
    assert _CountingStockModel.built == 1
    assert _CountingStockModel.token_calls == [(([{"role": "user", "content": []}], None), {})]
    # a second call reuses the built model
    assert asyncio.run(model.count_tokens([])) == 42 and _CountingStockModel.built == 1


@pytest.mark.siliconflow
def test_dropped_canvas_kwargs_are_named_in_one_warning(monkeypatch, caplog):
    """(c) The canvas emits Bedrock-only kwargs regardless of the node's model; dropping
    them silently hid why a `cache_config` never took effect. One WARNING names them —
    for an OpenAI-compatible row the full Bedrock-only set that was present, for a
    Bedrock row the platform-owned `region_name` / `boto_session`."""
    import logging

    from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

    namespace = _exec_adapted(monkeypatch, CANVAS_CODE)
    namespace["_identity_client"] = lambda: _FakeIdentity()
    BedrockAgentCoreContext.set_workload_access_token("wl-token")

    with caplog.at_level(logging.WARNING, logger="launchpad.studio.model"):
        _first(namespace["writer_model"].stream([]))
        _first(namespace["researcher_model"].stream([]))
        # a model whose canvas kwargs are all accepted logs nothing
        clean = namespace["BedrockModel"](model_id="global.moonshotai.kimi-k3", max_tokens=16)
        _first(clean.stream([]))
    warnings = [r for r in caplog.records if r.name == "launchpad.studio.model"]
    assert len(warnings) == 2
    openai_row, bedrock_row = warnings
    assert SF in openai_row.getMessage()
    assert "['cache_config', 'region_name']" in openai_row.getMessage()
    assert "rejects Bedrock-only fields" in openai_row.getMessage()
    assert "global.moonshotai.kimi-k3" in bedrock_row.getMessage()
    assert "['region_name']" in bedrock_row.getMessage()
    assert "MODEL_REGION applies" in bedrock_row.getMessage()


def test_entrypoint_captures_the_workload_token_eagerly(monkeypatch):
    """(d) Mirrors the strands template's prefetch: `invoke()` copies the request's
    injected token into the process slot BEFORE `main()` runs, so a model first used
    from a thread without the SDK context (a judge pool) still finds it."""
    from bedrock_agentcore.runtime.context import BedrockAgentCoreContext

    code = CANVAS_CODE.replace(
        "async def main(user_input, messages):\n    print(user_input)\n",
        "async def main(user_input, messages):\n"
        "    print(_WORKLOAD_TOKEN.get('value') or 'no-token')\n",
    )
    namespace = _exec_adapted(monkeypatch, code)
    assert "globals().get(\"_capture_workload_token\", lambda: None)()" in adapt_studio_code(code)
    namespace["_WORKLOAD_TOKEN"].pop("value", None)
    BedrockAgentCoreContext.set_workload_access_token("wl-eager")
    result = asyncio.run(namespace["invoke"]({"prompt": "hi"}))
    assert result == {"result": "wl-eager"}
    assert namespace["_WORKLOAD_TOKEN"]["value"] == "wl-eager"


def test_entrypoint_without_a_model_prelude_still_invokes(monkeypatch):
    """(d) A module that never constructs BedrockModel carries no prelude and therefore
    no `_capture_workload_token`; the eager hook is a no-op there, not a NameError."""
    code = "async def main(user_input, messages):\n    print('plain', user_input)\n"
    adapted = adapt_studio_code(code)
    assert "_LaunchpadStudioModel" not in adapted
    namespace: dict = {}
    exec(compile(adapted, "plain.py", "exec"), namespace)
    assert asyncio.run(namespace["invoke"]({"prompt": "x"})) == {"result": "plain x"}
