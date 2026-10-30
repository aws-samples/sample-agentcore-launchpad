"""D21 (M1-d): the generated runtimes read the model DESCRIPTOR first and fall back to the
inlined provider table — exercised on the Harness `model/load.py` (the sliced block + a
header), executed in-process with the SDK / strands imports stubbed."""

import importlib.util
import json
import sys
import types

import pytest

from app.schemas.agent import AgentSpec, catalog_version, model_descriptor
from app.templates import model_support

GLM = "global.zai.glm-5.3"
KIMI = "global.moonshotai.kimi-k3"


def _stub_modules(monkeypatch):
    ctx = types.ModuleType("bedrock_agentcore.runtime.context")
    ctx.BedrockAgentCoreContext = types.SimpleNamespace(
        get_workload_access_token=staticmethod(lambda: None),
        get_request_headers=staticmethod(lambda: {}),
    )
    runtime = types.ModuleType("bedrock_agentcore.runtime")
    runtime.context = ctx
    pkg = types.ModuleType("bedrock_agentcore")
    pkg.runtime = runtime
    models = types.ModuleType("strands.models")

    class BedrockModel:
        def __init__(self, **config):
            self.config = config

    models.BedrockModel = BedrockModel
    strands = types.ModuleType("strands")
    strands.models = models
    for name, mod in (("bedrock_agentcore", pkg), ("bedrock_agentcore.runtime", runtime),
                      ("bedrock_agentcore.runtime.context", ctx), ("strands", strands),
                      ("strands.models", models)):
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.delitem(sys.modules, "strands.models.openai", raising=False)


def _load(tmp_path, monkeypatch, model_id=GLM, env=None):
    _stub_modules(monkeypatch)
    if env is None:
        monkeypatch.delenv("LAUNCHPAD_MODEL_DESCRIPTOR", raising=False)
    else:
        monkeypatch.setenv("LAUNCHPAD_MODEL_DESCRIPTOR", env)
    spec = AgentSpec(name="probe", method="harness", system_prompt="p", model_id=model_id)
    path = tmp_path / "load.py"
    path.write_text(model_support.render_model_load(spec), encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location(f"load_{tmp_path.name}", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_rendered_descriptor_is_registered_and_read_before_the_table(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    assert mod.MODEL_CATALOG_VERSION == catalog_version()
    assert mod.MODEL_DESCRIPTOR == model_descriptor(GLM)
    assert mod.MODEL_DESCRIPTORS == {GLM: model_descriptor(GLM)}
    # the descriptor IS the provider row (same keys the dispatch reads, plus more)
    row = mod._model_provider(GLM)
    assert row is mod.MODEL_DESCRIPTORS[GLM]
    assert row["kind"] == "bedrock" and row["credential_provider"] == "launchpad-global-bedrock"
    # another catalog id without a descriptor → the inlined table; unknown → Bedrock default
    assert mod._model_provider(KIMI) == mod.MODEL_PROVIDERS[KIMI]
    assert mod._model_provider("custom.model")["kind"] == "bedrock"


def test_env_descriptor_overrides_the_rendered_row_and_adds_models(tmp_path, monkeypatch):
    override = {**model_descriptor(GLM), "inference_params": {"temperature": False, "top_p": False}}
    extra = {"model_id": "acme.foo", "kind": "openai_compatible", "base_url": "https://x/v1",
             "api_model_name": "foo", "credential_provider": "launchpad-acme",
             "streaming": False, "catalog_version": 99}
    mod = _load(tmp_path, monkeypatch, env=json.dumps([override, extra]))
    # same version → the env row replaces the rendered one (operator override wins)
    assert mod._model_provider(GLM)["inference_params"] == {"temperature": False, "top_p": False}
    assert mod._supported_model_config({"model_id": GLM, "temperature": 0.2, "max_tokens": 5}
                                       ) == {"model_id": GLM, "max_tokens": 5}
    # a model the table does not know is served from its descriptor
    assert mod._model_provider("acme.foo") is mod.MODEL_DESCRIPTORS["acme.foo"]
    assert mod._model_provider("acme.foo")["kind"] == "openai_compatible"


def test_env_descriptor_accepts_a_single_object_and_a_map(tmp_path, monkeypatch):
    single = {"model_id": "one", "kind": "bedrock", "catalog_version": 3}
    mod = _load(tmp_path, monkeypatch, env=json.dumps(single))
    assert "one" in mod.MODEL_DESCRIPTORS
    mapping = {"two": {"model_id": "two", "kind": "bedrock"}, "junk": "ignored", "bad": {"x": 1}}
    assert mod._launchpad_register_descriptors(mapping) == 1
    assert set(mod.MODEL_DESCRIPTORS) == {GLM, "one", "two"}


def test_a_strictly_newer_catalog_version_wins_an_older_one_loses(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch)
    rendered_version = mod.MODEL_DESCRIPTORS[GLM]["catalog_version"]
    older = {**model_descriptor(GLM), "catalog_version": rendered_version - 1, "streaming": False}
    assert mod._launchpad_register_descriptors(older) == 0
    assert mod.MODEL_DESCRIPTORS[GLM]["streaming"] is True
    newer = {**model_descriptor(GLM), "catalog_version": rendered_version + 1, "streaming": False}
    assert mod._launchpad_register_descriptors(newer) == 1
    assert mod.MODEL_DESCRIPTORS[GLM]["streaming"] is False
    assert mod._launchpad_register_descriptors(None) == 0
    assert mod._launchpad_register_descriptors([]) == 0


def test_table_fallback_for_inference_params_when_no_descriptor(tmp_path, monkeypatch):
    mod = _load(tmp_path, monkeypatch, model_id=KIMI)
    # Kimi's descriptor says temperature False (N12-bug11) — same answer as the table
    assert mod._supported_model_config({"model_id": KIMI, "temperature": 0.1}) == {"model_id": KIMI}
    del mod.MODEL_DESCRIPTORS[KIMI]
    assert mod._supported_model_config({"model_id": KIMI, "temperature": 0.1}) == {"model_id": KIMI}
    # an id outside both keeps every field
    assert mod._supported_model_config({"model_id": "custom", "temperature": 0.1}) == {
        "model_id": "custom", "temperature": 0.1}


def test_malformed_env_descriptor_fails_loudly_at_import(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="LAUNCHPAD_MODEL_DESCRIPTOR is not valid JSON"):
        _load(tmp_path, monkeypatch, env="{not json")


def test_chat_model_dispatch_reads_the_descriptor(tmp_path, monkeypatch):
    """An OpenAI-compatible descriptor for an id the table calls Bedrock routes to
    `_launchpad_openai_model` with the descriptor's endpoint and credential provider."""
    env = {"model_id": GLM, "kind": "openai_compatible", "base_url": "https://gw.example/v1",
           "api_model_name": "glm-mirror", "credential_provider": "launchpad-gw",
           "streaming": False, "catalog_version": 10 ** 6}
    mod = _load(tmp_path, monkeypatch, env=json.dumps(env))
    seen = {}

    def fake_openai(**kw):
        seen.update(kw)
        return "openai-model"

    monkeypatch.setattr(mod, "_launchpad_openai_model", fake_openai)
    monkeypatch.setattr(mod, "_resolve_api_key", lambda name: f"key-for-{name}")
    assert mod._launchpad_chat_model(model_id=GLM, max_tokens=12) == "openai-model"
    assert seen["base_url"] == "https://gw.example/v1" and seen["api_model_name"] == "glm-mirror"
    assert seen["api_key"] == "key-for-launchpad-gw" and seen["streaming"] is False
