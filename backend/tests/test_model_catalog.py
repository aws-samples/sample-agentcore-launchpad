"""Spec C3: the CN build serves exactly two Global Bedrock models, GLM 5.3 by default,
and the schema — not IAM — is what refuses everything else.

R13 (N13-obs7): SiliconFlow is no longer seeded; the module is marked `siliconflow`, so every
test sees the provider + its four historical rows the way an administrator registers them
(`tests/model_fixtures.py`), while the built-in snapshot itself is Bedrock-only.
"""

import pytest
from pydantic import ValidationError

from app.schemas import agent as schema
from app.schemas.agent import (
    DEFAULT_MODEL_CREDENTIAL,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_REGION,
    DEFAULT_REASONING_MAX_TOKENS,
    DEFAULT_RUNTIME_MAX_TOKENS,
    INFERENCE_PARAMS,
    MIN_MODEL_MAX_TOKENS,
    MODEL_NOT_ALLOWED_CODE,
    AgentSpec,
    allowed_model_ids,
    assert_model_allowed,
    default_runtime_max_tokens,
    inference_param_support,
    model_capabilities,
    model_default_max_tokens,
    studio_node_models,
    supported_model_config,
    supports_native_images,
)
from tests.model_fixtures import SILICONFLOW_MODELS, SILICONFLOW_PROVIDER, publish_siliconflow

pytestmark = pytest.mark.siliconflow

BASE = {"name": "catalog-probe", "method": "zip_runtime", "system_prompt": "p"}


@pytest.fixture(autouse=True)
def _builtin_snapshot():
    """C5 (M1-e): a test that publishes a variant snapshot (no module-level overlay exists any
    more) must hand the next test the built-in table back."""
    from app.schemas import model_catalog

    yield
    model_catalog.reset()


BEDROCK_IDS = ("global.zai.glm-5.3", "global.moonshotai.kimi-k3")
# the built-in snapshot + the admin-registered SiliconFlow rows (tests never build the app here)
publish_siliconflow()
ALLOWED_MODEL_IDS = allowed_model_ids()
MODEL_CAPABILITIES = model_capabilities()
MODEL_DEFAULT_MAX_TOKENS = model_default_max_tokens()
from app.schemas import model_catalog as _mc  # noqa: E402

_mc.reset()  # the `siliconflow` marker republishes per test


def test_catalog_is_the_two_global_models_plus_the_registered_siliconflow_rows_with_glm_default():
    # N-R6: the Bedrock rows are fixed; R13: the SiliconFlow rows are admin-registered, the
    # built-in seed itself is Bedrock-only
    assert ALLOWED_MODEL_IDS[:2] == BEDROCK_IDS
    assert all(m.startswith("siliconflow.") for m in ALLOWED_MODEL_IDS[2:])
    assert _mc.BUILTIN_CATALOG.allowed_ids() == BEDROCK_IDS
    assert set(_mc.BUILTIN_CATALOG.providers) == {"bedrock"}
    assert set(ALLOWED_MODEL_IDS) == set(schema.model_catalog_table())
    assert DEFAULT_MODEL_ID == "global.zai.glm-5.3"
    assert DEFAULT_MODEL_REGION == "us-west-2"
    assert DEFAULT_MODEL_CREDENTIAL == "launchpad-global-bedrock"
    assert MIN_MODEL_MAX_TOKENS == 256
    assert DEFAULT_RUNTIME_MAX_TOKENS >= MIN_MODEL_MAX_TOKENS
    assert schema.ModelSource.__args__ == ("bedrock",)
    assert MODEL_NOT_ALLOWED_CODE == "model.not_allowed"


def test_spec_defaults_carry_region_and_credential():
    spec = AgentSpec(**BASE)
    assert (spec.model_id, spec.model_source) == (DEFAULT_MODEL_ID, "bedrock")
    assert spec.model_region == "us-west-2"
    assert spec.model_credential == "launchpad-global-bedrock"
    # stored specs written before the fields existed read back with the defaults
    assert AgentSpec(**{**BASE, "model_id": DEFAULT_MODEL_ID}).model_credential == (
        DEFAULT_MODEL_CREDENTIAL
    )


@pytest.mark.parametrize("model_id", ALLOWED_MODEL_IDS)
def test_every_allowed_id_validates(model_id):
    assert AgentSpec(**BASE, model_id=model_id).model_id == model_id
    assert assert_model_allowed(model_id) == model_id


@pytest.mark.parametrize(
    "bad",
    [
        "global.anthropic.claude-sonnet-5",
        "us.zai.glm-5.3",  # the US profile is not the Global one
        "openai.gpt-5.6-sol",
        "arn:aws-cn:bedrock:cn-northwest-1:111122223333:inference-profile/x",
        "",
    ],
)
def test_any_other_model_id_is_refused_with_the_code(bad):
    with pytest.raises(ValidationError) as exc:
        AgentSpec(**BASE, model_id=bad)
    message = str(exc.value)
    assert MODEL_NOT_ALLOWED_CODE in message
    assert "global.zai.glm-5.3" in message and "global.moonshotai.kimi-k3" in message


@pytest.mark.parametrize(
    "field, value",
    [("model_region", "US_WEST"), ("model_region", ""), ("model_credential", "has space"),
     ("model_credential", "")],
)
def test_region_and_credential_shapes_are_validated(field, value):
    with pytest.raises(ValidationError):
        AgentSpec(**{**BASE, field: value})


def test_capability_table_covers_the_whole_catalog_and_only_kimi_has_vision():
    assert set(MODEL_CAPABILITIES) == set(ALLOWED_MODEL_IDS)
    assert supports_native_images("global.moonshotai.kimi-k3") is True
    assert supports_native_images("global.zai.glm-5.3") is False
    assert not any(supports_native_images(m) for m in ALLOWED_MODEL_IDS[2:])  # defaults
    # an id outside the catalog is conservatively non-vision, never a KeyError
    assert supports_native_images("unknown-model") is False


def test_capability_table_says_which_inference_params_each_model_accepts():
    """N12-bug11 (live 2026-10-10): Kimi K3 rejects `temperature` on ConverseStream, GLM 5.3
    accepts it — the one table answers every INFERENCE_PARAMS key for every catalog id."""
    assert INFERENCE_PARAMS == ("temperature",)
    for model_id in ALLOWED_MODEL_IDS:
        assert set(INFERENCE_PARAMS) <= set(MODEL_CAPABILITIES[model_id]), model_id
    assert inference_param_support("global.moonshotai.kimi-k3") == {"temperature": False}
    assert inference_param_support("global.zai.glm-5.3") == {"temperature": True}
    config = {"max_tokens": 1024, "temperature": 0.0}
    assert supported_model_config("global.moonshotai.kimi-k3", config) == {"max_tokens": 1024}
    assert supported_model_config("global.zai.glm-5.3", config) == config
    # an id outside the catalog is never pruned — the field is sent as before
    assert supported_model_config("unknown-model", config) == config
    assert inference_param_support("unknown-model") == {"temperature": True}


def test_region_and_credential_can_be_overridden():
    spec = AgentSpec(**BASE, model_region="us-east-1", model_credential="team-bedrock-key")
    assert (spec.model_region, spec.model_credential) == ("us-east-1", "team-bedrock-key")


def _studio(*nodes: dict) -> dict:
    flow = {"nodes": list(nodes), "edges": [], "graphMode": False}
    return {**BASE, "method": "studio", "code": "print(1)", "studio_flow": flow}


def _node(node_type: str = "agent", **data) -> dict:
    return {"id": node_type, "type": node_type, "data": data}


def test_studio_node_models_lists_only_explicit_catalog_branch_ids():
    flow = _studio(
        _node(modelProvider="AWS Bedrock", modelId="global.moonshotai.kimi-k3"),
        _node("orchestrator-agent", modelId="global.zai.glm-5.3"),  # provider absent = Bedrock
        _node("swarm"),  # no modelId ⇒ the canvas default, nothing to check
        _node("tool", modelId="not-a-model-node"),
        _node(modelProvider="OpenAI", modelName="gpt-4o", modelId="gpt-4o"),
    )["studio_flow"]
    assert studio_node_models(flow) == [
        "global.moonshotai.kimi-k3", "global.zai.glm-5.3"
    ]
    assert studio_node_models(None) == [] and studio_node_models("x") == []


def test_studio_flow_nodes_are_held_to_the_catalog():
    AgentSpec(**_studio(_node(modelId="global.moonshotai.kimi-k3"), _node("swarm")))
    with pytest.raises(ValidationError, match=MODEL_NOT_ALLOWED_CODE):
        AgentSpec(**_studio(_node("orchestrator-agent",
                                  modelId="global.anthropic.claude-sonnet-5")))
    # a zip_runtime spec that happens to carry a studio_flow is not a canvas publish
    AgentSpec(**BASE, studio_flow={"nodes": [_node(modelId="global.anthropic.claude-opus-5")]})


def test_default_max_tokens_is_model_aware_and_covers_the_whole_catalog():
    # N10-bug4 (live): GLM 5.3 / Kimi K3 need ≥ 16k once a skill is mounted; a model
    # outside the catalog keeps the historical 4096 (Bedrock rejects over-limit values).
    assert set(MODEL_DEFAULT_MAX_TOKENS) == set(ALLOWED_MODEL_IDS)
    assert DEFAULT_REASONING_MAX_TOKENS >= 16384
    for model_id in BEDROCK_IDS:
        assert default_runtime_max_tokens(model_id) == DEFAULT_REASONING_MAX_TOKENS
    for model_id in ALLOWED_MODEL_IDS[2:]:  # N-R6: SiliconFlow rows default to 8192
        assert default_runtime_max_tokens(model_id) == schema.DEFAULT_OPENAI_COMPATIBLE_MAX_TOKENS
    assert default_runtime_max_tokens("custom.byoc-model") == DEFAULT_RUNTIME_MAX_TOKENS == 4096
    assert DEFAULT_RUNTIME_MAX_TOKENS >= MIN_MODEL_MAX_TOKENS
    assert all(v <= schema.MAX_TOKENS_CEILING for v in MODEL_DEFAULT_MAX_TOKENS.values())


# ─── N-R6: provider dimension ────────────────────────────────────────────────────


SF = "siliconflow.zai-org/GLM-5.3"


def test_providers_table_names_the_siliconflow_endpoint_credential_and_parameter():
    sf = schema.providers()["siliconflow"]
    assert sf["kind"] == "openai_compatible"
    assert sf["base_url"] == "https://api.siliconflow.cn/v1"
    assert sf["credential_provider"] == "launchpad-siliconflow"
    assert sf["ssm_parameter"] == "launchpad-siliconflow-api-key"
    assert sf["api_key_env"] == "LAUNCHPAD_SILICONFLOW_API_KEY"
    assert sf["required"] is False and sf["id_prefix"] == "siliconflow."
    bedrock = schema.providers()["bedrock"]
    assert bedrock["kind"] == "bedrock" and bedrock["base_url"] is None
    assert bedrock["credential_provider"] == DEFAULT_MODEL_CREDENTIAL and bedrock["required"]
    assert bedrock["ssm_parameter"] == "launchpad-global-bedrock-api-key"
    assert all(row["credential_provider"].startswith("launchpad-")
               for row in schema.providers().values())


def test_registered_siliconflow_rows_resolve_to_the_provider():
    # C5 (M1-e): `Settings.siliconflow_models` / LAUNCHPAD_SILICONFLOW_MODELS no longer exist;
    # R13 (N13-obs7): nor does a SiliconFlow seed — the rows are the administrator's
    from app.core.config import Settings

    assert "siliconflow_models" not in Settings.model_fields
    assert not hasattr(schema, "DEFAULT_SILICONFLOW_MODELS")
    defaults = [name for name, _ in schema.parse_provider_models(SILICONFLOW_MODELS)]
    # Newest generation per vendor as of 2026-10-12 (siliconflow.cn/pricing); the catalog's
    # OpenAI-compatible defaults (max 8192 / judge 4096) are sane for these reasoning models.
    assert defaults == ["zai-org/GLM-5.3", "deepseek-ai/DeepSeek-V4-Pro", "Qwen/Qwen3.8-27B",
                        "Pro/moonshotai/Kimi-K2.6"]
    for name in defaults:
        model_id = f"siliconflow.{name}"
        assert model_id in ALLOWED_MODEL_IDS
        entry = schema.model_entry(model_id)
        assert entry["provider"] == "siliconflow" and entry["kind"] == "openai_compatible"
        assert entry["base_url"] == "https://api.siliconflow.cn/v1"
        assert entry["credential_provider"] == "launchpad-siliconflow"
        assert entry["api_model_name"] == name == schema.api_model_name(model_id)
        assert schema.supports_tools(model_id) and entry["streaming"]
        assert schema.is_openai_compatible_model(model_id)
        assert schema.model_provider(model_id) is schema.providers()["siliconflow"]
    assert schema.model_provider("global.zai.glm-5.3") is schema.providers()["bedrock"]
    assert schema.model_provider("custom.byoc") is schema.providers()["bedrock"]
    assert schema.api_model_name("global.zai.glm-5.3") == "global.zai.glm-5.3"


def test_every_catalog_row_answers_the_provider_traits():
    for model_id in ALLOWED_MODEL_IDS:
        entry = schema.model_entry(model_id)
        for key in ("provider", "kind", "label", "api_model_name", "base_url",
                    "credential_provider", "vision", "temperature", "tools", "streaming",
                    "default_max_tokens", "default_judge_max_tokens"):
            assert key in entry, (model_id, key)
        assert entry["provider"] in schema.providers()
        assert entry["kind"] == schema.providers()[entry["provider"]]["kind"]
        assert set(INFERENCE_PARAMS) <= set(MODEL_CAPABILITIES[model_id])
        assert {"tools", "streaming"} <= set(MODEL_CAPABILITIES[model_id])
    table = schema.model_provider_table()
    assert set(table) == set(ALLOWED_MODEL_IDS)
    assert table["global.zai.glm-5.3"]["base_url"] is None
    assert table[SF]["base_url"] == "https://api.siliconflow.cn/v1"
    assert table[SF]["credential_provider"] == "launchpad-siliconflow"


def test_override_grammar_builds_catalogs_and_refuses_typos():
    spec = ("Qwen/Qwen3-8B:notools+max=4096+judge=2048, zai-org/GLM-4.5V:vision , "
            "deepseek-ai/DeepSeek-V3")
    with pytest.raises(ValueError, match="provider_row"):  # R13: no built-in OpenAI provider
        schema.build_model_catalog(spec)
    catalog = schema.build_model_catalog(spec, SILICONFLOW_PROVIDER)
    assert list(catalog)[:2] == list(BEDROCK_IDS)
    small = catalog["siliconflow.Qwen/Qwen3-8B"]
    assert small["tools"] is False and small["default_max_tokens"] == 4096
    assert small["default_judge_max_tokens"] == 2048 and small["vision"] is False
    vision = catalog["siliconflow.zai-org/GLM-4.5V"]
    assert vision["vision"] is True and vision["tools"] is True and vision["streaming"] is True
    plain = catalog["siliconflow.deepseek-ai/DeepSeek-V3"]
    assert plain["default_max_tokens"] == schema.DEFAULT_OPENAI_COMPATIBLE_MAX_TOKENS
    assert plain["label"] == "DeepSeek-V3 (SiliconFlow)"
    # empty override ⇒ Bedrock only; the two Bedrock rows never change
    assert tuple(schema.build_model_catalog("")) == BEDROCK_IDS
    for bad in ("Qwen/Qwen3-8B:turbo", "Qwen/Qwen3-8B:max=12", "Qwen/Qwen3-8B:max=abc", ":tools",
                "Qwen/Qwen 3"):
        with pytest.raises(ValueError, match="model catalog override"):
            schema.parse_provider_models(bad)


def test_siliconflow_model_is_an_agent_model_when_it_has_tools_and_judge_only_otherwise(
    monkeypatch,
):
    spec = AgentSpec(**BASE, model_id=SF)
    assert spec.model_id == SF and spec.model_credential == DEFAULT_MODEL_CREDENTIAL
    # a judge-only row: allowed in the catalog, refused as an agent model with its own code
    judge_only = "siliconflow.Qwen/Qwen3-8B"
    # C5 (M1-e): publish the row into the snapshot (no module constant to overlay)
    from app.schemas import model_catalog

    entry = schema.openai_compatible_entry("siliconflow", "Qwen/Qwen3-8B", {"tools": False},
                                           provider_row=SILICONFLOW_PROVIDER)
    model_catalog.publish(model_catalog.extended(
        model_catalog.current(),
        models={judge_only: model_catalog.model_row_from_legacy(entry, source="manual")}))
    assert schema.model_capabilities()[judge_only]["tools"] is False
    assert assert_model_allowed(judge_only) == judge_only  # judges go through this one
    with pytest.raises(ValueError, match=schema.MODEL_TOOLS_UNSUPPORTED_CODE):
        schema.assert_agent_model_allowed(judge_only)
    with pytest.raises(ValidationError) as exc:
        AgentSpec(**BASE, model_id=judge_only)
    assert "model.tools_unsupported" in str(exc.value) and "tool calling" in str(exc.value)
    with pytest.raises(ValidationError, match="model.tools_unsupported"):
        AgentSpec(**{**BASE, "method": "byoc", "system_prompt": ""},
                  byoc={"artifact_kind": "code_zip", "upload_id": "u1",
                        "allowed_models": [judge_only]})


def test_studio_nodes_accept_any_catalog_model_and_are_held_to_a_configured_provider(
        monkeypatch):
    """N-R6-obs2: the canvas is no longer Bedrock-only — a node's OpenAI-compatible id parses
    like `model_id` does, and the request-boundary guard refuses it only while its provider
    has no key (422 `model.provider_unconfigured`, naming the SSM parameter)."""
    monkeypatch.delenv(SF_ENV, raising=False)
    spec = AgentSpec(**_studio(_node(modelId=SF), _node("swarm", modelId="global.zai.glm-5.3")))
    assert studio_node_models(spec.studio_flow) == [SF, "global.zai.glm-5.3"]
    with pytest.raises(schema.ModelRefusal) as exc_info:
        schema.assert_spec_models_available(spec)
    assert exc_info.value.code == schema.MODEL_PROVIDER_UNCONFIGURED_CODE
    assert "studio node modelId" in exc_info.value.reason
    assert "launchpad-siliconflow-api-key" in exc_info.value.reason
    # configured (bootstrap record on the workspace map) → passes; the judge-only rule holds
    schema.assert_spec_models_available(
        spec, {"model_credentials": {"siliconflow": {"configured": True}}})
    monkeypatch.setenv(SF_ENV, "k")
    schema.assert_spec_models_available(spec)  # the hub env alone configures it too


def test_byoc_may_carry_a_siliconflow_model_without_a_bedrock_resource():
    assert schema.byoc_model_target(SF) == ("openai_compatible", "zai-org/GLM-5.3")
    spec = AgentSpec(**{**BASE, "method": "byoc", "system_prompt": ""},
                     byoc={"artifact_kind": "code_zip", "upload_id": "u1",
                           "allowed_models": [SF, "global.zai.glm-5.3"]})
    assert spec.allowed_model_ids == [SF, "global.zai.glm-5.3"]


def test_catalog_entries_and_prose_carry_the_provider():
    entries = {e["model_id"]: e for e in schema.model_catalog_entries()}
    assert entries[SF]["provider"] == "siliconflow"
    assert entries[SF]["provider_label"] == "SiliconFlow"
    assert entries[SF]["provider_kind"] == "openai_compatible" and entries[SF]["tools"] is True
    assert entries["global.zai.glm-5.3"]["provider_kind"] == "bedrock"
    line = next(line for line in schema.model_catalog_lines() if f"`{SF}`" in line)
    assert "provider: SiliconFlow (OpenAI-compatible endpoint, model `zai-org/GLM-5.3`)" in line
    assert "tool calling: yes" in line


# ─── N-R6-obs1: a model whose provider has no key is marked and refused at selection ────
SF_ENV = "LAUNCHPAD_SILICONFLOW_API_KEY"


@pytest.fixture
def sf_unconfigured(monkeypatch):
    """No hub key (the hermetic default; made explicit) — and no workspace map passed."""
    monkeypatch.delenv(SF_ENV, raising=False)


def test_provider_configured_prefers_the_bootstrap_record_over_the_hub_env(sf_unconfigured,
                                                                             monkeypatch):
    assert schema.provider_configured("bedrock") is True  # required: always
    assert schema.provider_configured("siliconflow") is False
    # the hub env alone (pre-record yaml) configures it
    monkeypatch.setenv(SF_ENV, "k")
    assert schema.provider_configured("siliconflow") is True
    # the bootstrap's record wins both ways: it says whether the Identity provider exists
    record = {"model_credentials": {"siliconflow": {"configured": False}}}
    assert schema.provider_configured("siliconflow", record) is False
    monkeypatch.delenv(SF_ENV)
    record["model_credentials"]["siliconflow"]["configured"] = True
    assert schema.provider_configured("siliconflow", record) is True
    # no map (None) = the hub's own environment — never a Settings read (workspace funnel)
    assert schema.configured_providers() == {"bedrock": True, "siliconflow": False}
    assert schema.configured_providers(record) == {"bedrock": True, "siliconflow": True}
    assert schema.model_available(SF, record) and schema.model_available("global.zai.glm-5.3")
    assert schema.model_available(SF) is False
    assert schema.model_available("legacy.custom") is True  # the catalog check refuses it


def test_unconfigured_provider_model_is_refused_with_the_code_and_the_ssm_parameter(
    sf_unconfigured,
):
    with pytest.raises(schema.ModelRefusal) as exc:
        schema.assert_model_available(SF, "judge model_id")
    assert exc.value.code == schema.MODEL_PROVIDER_UNCONFIGURED_CODE
    assert exc.value.code == "model.provider_unconfigured"
    message = str(exc.value)
    assert message.startswith("model.provider_unconfigured: judge model_id ")
    assert "SiliconFlow" in message and "launchpad-siliconflow-api-key" in message
    assert SF_ENV in message and "make bootstrap" in message
    assert schema.model_refusal_code(exc.value) == "model.provider_unconfigured"
    with pytest.raises(schema.ModelRefusal) as exc2:
        schema.assert_agent_model_available(SF)
    assert exc2.value.code == "model.provider_unconfigured"
    # the catalog refusal keeps its code and message, and a plain ValueError maps to it
    with pytest.raises(schema.ModelRefusal) as exc3:
        schema.assert_model_available("global.anthropic.claude-opus-5")
    assert exc3.value.code == MODEL_NOT_ALLOWED_CODE
    assert schema.model_refusal_code(ValueError("x")) == MODEL_NOT_ALLOWED_CODE
    # Bedrock models are always available
    assert schema.assert_model_available("global.zai.glm-5.3") == "global.zai.glm-5.3"
    assert schema.assert_agent_model_available("global.moonshotai.kimi-k3")


def test_unconfigured_provider_model_passes_once_the_key_is_configured(sf_unconfigured,
                                                                        monkeypatch):
    monkeypatch.setenv(SF_ENV, "k")
    assert schema.assert_model_available(SF) == SF
    assert schema.assert_agent_model_available(SF) == SF
    record = {"model_credentials": {"siliconflow": {"configured": True}}}
    monkeypatch.delenv(SF_ENV)
    assert schema.assert_model_available(SF, resources=record) == SF


def test_agent_spec_still_parses_an_unconfigured_provider_model_but_the_guard_refuses_it(
    sf_unconfigured,
):
    """Stored specs (pipeline resume, canary, optimization) must keep parsing when a
    provider's key is rotated away — the refusal is the request-boundary guard's."""
    spec = AgentSpec(**BASE, model_id=SF)
    assert spec.model_id == SF
    with pytest.raises(schema.ModelRefusal) as exc:
        schema.assert_spec_models_available(spec)
    assert exc.value.code == "model.provider_unconfigured" and "model_id" in str(exc.value)
    byoc = AgentSpec(name="byoc-probe", method="byoc", system_prompt="p",
                     model_id="global.zai.glm-5.3",
                     byoc={"artifact_kind": "code_zip", "upload_id": "u1",
                           "allowed_models": ["global.zai.glm-5.3", SF]})
    with pytest.raises(schema.ModelRefusal) as exc2:
        schema.assert_spec_models_available(byoc)
    assert "byoc.allowed_models entry" in str(exc2.value)
    # a workspace whose record says configured passes the same spec
    record = {"model_credentials": {"siliconflow": {"configured": True}}}
    schema.assert_spec_models_available(spec, record)
    schema.assert_spec_models_available(byoc, record)
    schema.assert_spec_models_available(AgentSpec(**BASE))  # Bedrock default: always


def test_catalog_entries_and_prose_mark_an_unconfigured_provider_model(sf_unconfigured,
                                                                        monkeypatch):
    entries = {e["model_id"]: e for e in schema.model_catalog_entries()}
    assert entries["global.zai.glm-5.3"]["available"] is True
    assert entries[SF]["available"] is False
    line = next(line for line in schema.model_catalog_lines() if f"`{SF}`" in line)
    assert line.startswith(f"- `{SF}` {schema.MODEL_UNAVAILABLE_MARKER} — ")
    assert "(not configured in this workspace — not selectable)" in line
    glm = next(line for line in schema.model_catalog_lines() if "`global.zai.glm-5.3`" in line)
    assert schema.MODEL_UNAVAILABLE_MARKER not in glm and glm.startswith(
        "- `global.zai.glm-5.3` (default) — ")
    # configured (env or record) → no marker, available
    monkeypatch.setenv(SF_ENV, "k")
    assert {e["model_id"]: e["available"] for e in schema.model_catalog_entries()}[SF] is True
    assert all(schema.MODEL_UNAVAILABLE_MARKER not in line for line in schema.model_catalog_lines())
    monkeypatch.delenv(SF_ENV)
    record = {"model_credentials": {"siliconflow": {"configured": True}}}
    assert all(schema.MODEL_UNAVAILABLE_MARKER not in line
               for line in schema.model_catalog_lines(record))
