"""N12 E0-5 — every judge / actor / optimizer model is built in ONE place, on the Global
Bedrock bearer (spec C3/C4; plan §B.2 "judge model factory", risk R-3)."""

import pytest
from botocore.config import Config

from app.core.errors import AppError
from app.services import aws_clients

BEARER = "AWS_BEARER_TOKEN_BEDROCK"


@pytest.fixture
def bearer(monkeypatch):
    monkeypatch.setenv(BEARER, "bedrock-api-key")
    aws_clients.reset_cache()
    yield
    aws_clients.reset_cache()


def test_returns_a_bedrock_model_on_the_global_bearer_endpoint(bearer):
    from strands.models import BedrockModel

    model = aws_clients.global_bedrock_model("global.zai.glm-5.3", region="us-west-2")
    assert isinstance(model, BedrockModel)
    assert model.client.meta.endpoint_url.endswith("bedrock-runtime.us-west-2.amazonaws.com")
    assert model.client.meta.config.signature_version == "bearer"
    assert isinstance(model.client.meta.config, Config)
    cfg = model.get_config()
    assert cfg["model_id"] == "global.zai.glm-5.3"
    assert cfg["max_tokens"] == 1024 and cfg["temperature"] == 0.0


def test_max_tokens_floor_is_the_catalog_minimum(bearer):
    from app.schemas.agent import MIN_MODEL_MAX_TOKENS

    model = aws_clients.global_bedrock_model("global.moonshotai.kimi-k3", region="us-west-2",
                                             max_tokens=16)
    assert model.get_config()["max_tokens"] == MIN_MODEL_MAX_TOKENS


def test_kimi_k3_gets_no_temperature_and_glm_keeps_it(bearer):
    """N12-bug11 (t1 run 917fef1fe10e): Kimi K3 answers ConverseStream with
    ValidationException "This model doesn't support the temperature field" — the builder
    consults the catalog's capability table and omits the field for that model only."""
    kimi = aws_clients.global_bedrock_model("global.moonshotai.kimi-k3", region="us-west-2",
                                            temperature=0.0)
    cfg = kimi.get_config()
    assert "temperature" not in cfg
    assert cfg["model_id"] == "global.moonshotai.kimi-k3" and cfg["max_tokens"] == 1024
    glm = aws_clients.global_bedrock_model("global.zai.glm-5.3", region="us-west-2",
                                           temperature=1.0)
    assert glm.get_config()["temperature"] == 1.0


def test_refuses_models_outside_the_cn_catalog(bearer):
    with pytest.raises(AppError) as exc:
        aws_clients.global_bedrock_model("global.anthropic.claude-sonnet-5", region="us-west-2")
    assert exc.value.code == "model.not_allowed" and exc.value.status_code == 422


def test_refuses_to_build_without_the_bearer(monkeypatch):
    monkeypatch.delenv(BEARER, raising=False)
    aws_clients.reset_cache()
    with pytest.raises(RuntimeError, match=BEARER):
        aws_clients.global_bedrock_model("global.zai.glm-5.3", region="us-west-2")


def test_model_id_is_never_defaulted(bearer):
    """A `model=None` anywhere downstream would make strands build a Claude default
    without the bearer — the factory has no default model id."""
    import inspect

    sig = inspect.signature(aws_clients.global_bedrock_model)
    assert sig.parameters["model_id"].default is inspect.Parameter.empty
    assert sig.parameters["region"].default is inspect.Parameter.empty


# ─── N-R6: provider dispatch ──────────────────────────────────────────────────────

SF = "siliconflow.zai-org/GLM-5.3"


class _FakeOpenAIModel:
    def __init__(self, *, client_args=None, **config):
        self.client_args = dict(client_args or {})
        self.config = dict(config)

    def get_config(self):
        return self.config


@pytest.fixture
def fake_openai(monkeypatch):
    import strands.models.openai as openai_models

    monkeypatch.setattr(openai_models, "OpenAIModel", _FakeOpenAIModel)
    return _FakeOpenAIModel


@pytest.mark.siliconflow
def test_an_openai_compatible_model_is_built_on_its_endpoint_with_its_own_key(
    bearer, fake_openai, monkeypatch
):
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", "sf-key-SECRET")
    model = aws_clients.build_chat_model(SF, region="us-west-2", max_tokens=2048,
                                         temperature=0.2, read_timeout_s=77)
    assert isinstance(model, fake_openai)
    assert model.client_args == {"base_url": "https://api.siliconflow.cn/v1",
                                 "api_key": "sf-key-SECRET", "timeout": 77, "max_retries": 1}
    # the endpoint receives the bare provider id, never the Launchpad id
    assert model.config["model_id"] == "zai-org/GLM-5.3"
    assert model.config["params"] == {"max_tokens": 2048, "temperature": 0.2}
    assert model.config["stream"] is True
    # the Bedrock bearer is untouched / not used for this provider
    assert "sf-key-SECRET" not in str(model.config)
    # the historical name is a thin alias
    alias = aws_clients.global_bedrock_model(SF, region="us-west-2")
    assert isinstance(alias, fake_openai) and alias.config["params"]["max_tokens"] == 1024


@pytest.mark.siliconflow
def test_openai_compatible_max_tokens_is_floored_like_bedrock(bearer, fake_openai, monkeypatch):
    from app.schemas.agent import MIN_MODEL_MAX_TOKENS

    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", "k")
    model = aws_clients.build_chat_model(SF, region="us-west-2", max_tokens=8)
    assert model.config["params"]["max_tokens"] == MIN_MODEL_MAX_TOKENS


@pytest.mark.siliconflow
def test_missing_provider_key_names_the_variable_and_the_ssm_parameter(bearer, fake_openai,
                                                                       monkeypatch):
    """N-R6-obs1: an unconfigured provider is a 422 `model.provider_unconfigured` (the
    console translates it) naming the SSM parameter and the variable — no RuntimeError."""
    from app.core.errors import AppError

    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    with pytest.raises(AppError) as exc:
        aws_clients.build_chat_model(SF, region="us-west-2")
    assert exc.value.code == "model.provider_unconfigured" and exc.value.status_code == 422
    assert "LAUNCHPAD_SILICONFLOW_API_KEY" in str(exc.value)
    assert "launchpad-siliconflow-api-key" in str(exc.value)
    # the hub builder answers for the hub's own environment: the key present → built
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", "k")
    assert isinstance(aws_clients.build_chat_model(SF, region="us-west-2"), fake_openai)
    # the Bedrock path is unaffected by the missing SiliconFlow key
    from strands.models import BedrockModel

    assert isinstance(aws_clients.build_chat_model("global.zai.glm-5.3", region="us-west-2"),
                      BedrockModel)


@pytest.mark.siliconflow
def test_require_provider_api_key_dispatches_bedrock_to_the_bearer(bearer, monkeypatch):
    assert aws_clients.require_provider_api_key("bedrock") == "bedrock-api-key"
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", " sf ")
    assert aws_clients.require_provider_api_key("siliconflow") == "sf"
