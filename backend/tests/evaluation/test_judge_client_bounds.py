"""Judge-client bounds (Round 3 follow-up): a stalled judge call holds its thread for about
1–2× the judge deadline, not botocore's / OpenAI's 15–30 min defaults
(``strands_eval/judge_client``). Client config only: the judge fingerprint is unchanged."""

from app.core.config import Settings
from app.services import aws_clients
from app.services.strands_eval import deadlines, judge_client
from tests.evaluation.fakes import FakeModel

DEADLINE = Settings.model_fields["eval_judge_deadline_s"].default


def test_a_stall_ends_within_two_judge_deadlines():
    assert DEADLINE == deadlines.DEFAULT_JUDGE_DEADLINE_S == 300
    assert judge_client.bedrock_stall_bound_s() == 2 * (150 + 10) <= 2 * DEADLINE
    assert judge_client.openai_stall_bound_s() == 2 * 300 <= 2 * DEADLINE


def test_the_hub_judge_client_uses_the_bounds(monkeypatch):
    built = {}

    class _Recorder(FakeModel):
        def __init__(self, **kwargs):
            super().__init__()
            built.update(kwargs)

    import strands.models as strands_models

    monkeypatch.setattr(strands_models, "BedrockModel", _Recorder)
    monkeypatch.setenv(aws_clients.GLOBAL_BEDROCK_BEARER_ENV, "test-bearer")
    aws_clients.build_chat_model("global.zai.glm-5.3", region="us-west-2", max_tokens=2048)
    config = built["boto_client_config"]
    assert (config.read_timeout, config.connect_timeout) == (150, 10)
    assert config.retries == {"total_max_attempts": 2, "mode": "standard"}
    assert config.signature_version == "bearer"
    built.clear()
    aws_clients.build_chat_model("global.zai.glm-5.3", region="us-west-2", max_tokens=2048,
                                 read_timeout_s=600)  # a caller's own timeout wins
    assert built["boto_client_config"].read_timeout == 600


def test_an_openai_compatible_judge_gets_the_request_bound():
    class _Model:
        client_args = {"base_url": "u", "api_key": "k"}

    model = judge_client.limit_openai_client(_Model())
    assert model.client_args == {"base_url": "u", "api_key": "k", "timeout": 300,
                                 "max_retries": 1}
    assert judge_client.limit_openai_client(object()) is not None  # other models pass
