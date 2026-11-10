"""Hermetic tests for the BYOC samples' CN model path (N10-bug3).

No AWS, no network, no real SDK: the ``bedrock_agentcore`` and ``boto3`` modules the
samples import are replaced by recording fakes *before* each sample is loaded, so the
tests prove the contract the code exercises — the Runtime workload token is exchanged
with ``GetResourceApiKey`` on ``LAUNCHPAD_MODEL_CREDENTIAL``, the key lands in
``AWS_BEARER_TOKEN_BEDROCK`` before the ``bedrock-runtime`` client exists, the client
targets ``LAUNCHPAD_MODEL_REGION`` and Converse is called on ``MODEL_ID`` — plus the
echo fallback and the cache behaviour. Run via ``samples/byoc/check.sh``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

SAMPLES = Path(__file__).resolve().parents[1]
MAINS = [SAMPLES / "hello-http" / "main.py", SAMPLES / "hello-container" / "main.py"]
BEARER_ENV = "AWS_BEARER_TOKEN_BEDROCK"


class Recorder:
    """Everything the fakes observed, asserted on by the tests."""

    def __init__(self) -> None:
        self.injected_token: str | None = None
        self.identity_regions: list[str] = []
        self.api_key_calls: list[dict] = []
        self.minted_workloads: list[str] = []
        self.api_key = "k-test-123"
        self.clients: list[dict] = []
        self.converse_calls: list[dict] = []
        self.converse_error: Exception | None = None
        self.stop_reason = "end_turn"
        self.converse_blocks = [
            {"reasoningContent": {"reasoningText": {"text": "thinking…"}}},
            {"text": "Hello from "},
            {"text": "the model"},
        ]


def _fake_modules(rec: Recorder) -> dict[str, types.ModuleType]:
    """Minimal stand-ins for the SDK surface the samples touch."""
    runtime = types.ModuleType("bedrock_agentcore.runtime")

    class BedrockAgentCoreApp:
        def entrypoint(self, fn):
            self.handler = fn
            return fn

        def run(self):  # pragma: no cover — never called in tests
            raise AssertionError("app.run() must not run under test")

    runtime.BedrockAgentCoreApp = BedrockAgentCoreApp

    context = types.ModuleType("bedrock_agentcore.runtime.context")

    class BedrockAgentCoreContext:
        @classmethod
        def get_workload_access_token(cls):
            return rec.injected_token

    context.BedrockAgentCoreContext = BedrockAgentCoreContext

    identity = types.ModuleType("bedrock_agentcore.services.identity")

    class _DataPlane:
        def get_resource_api_key(self, **kwargs):
            rec.api_key_calls.append(kwargs)
            return {"apiKey": rec.api_key}

    class IdentityClient:
        def __init__(self, region):
            rec.identity_regions.append(region)
            self.dp_client = _DataPlane()

        def get_workload_access_token(self, workload_name):
            rec.minted_workloads.append(workload_name)
            return {"workloadAccessToken": f"minted-for-{workload_name}"}

    identity.IdentityClient = IdentityClient

    boto3 = types.ModuleType("boto3")

    class _BedrockRuntime:
        def converse(self, **kwargs):
            rec.converse_calls.append(kwargs)
            if rec.converse_error is not None:
                raise rec.converse_error
            return {"output": {"message": {"content": rec.converse_blocks}},
                    "stopReason": rec.stop_reason}

    def client(service, **kwargs):
        # What matters is the environment *at construction* — botocore reads the
        # bearer then, not per call.
        rec.clients.append({"service": service, "bearer_at_init": os.environ.get(BEARER_ENV), **kwargs})
        return _BedrockRuntime()

    boto3.client = client

    config = types.ModuleType("botocore.config")

    class Config:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    config.Config = Config

    pkg = types.ModuleType("bedrock_agentcore")
    services = types.ModuleType("bedrock_agentcore.services")
    botocore = types.ModuleType("botocore")
    return {
        "bedrock_agentcore": pkg,
        "bedrock_agentcore.runtime": runtime,
        "bedrock_agentcore.runtime.context": context,
        "bedrock_agentcore.services": services,
        "bedrock_agentcore.services.identity": identity,
        "boto3": boto3,
        "botocore": botocore,
        "botocore.config": config,
    }


@pytest.fixture(params=MAINS, ids=[p.parent.name for p in MAINS])
def sample(request, monkeypatch):
    """Load one sample's ``main.py`` fresh (its key cache starts empty) over the fakes."""
    rec = Recorder()
    for name, module in _fake_modules(rec).items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.delenv(BEARER_ENV, raising=False)
    monkeypatch.delenv("LAUNCHPAD_WORKLOAD_NAME", raising=False)
    monkeypatch.setenv("AWS_REGION", "cn-northwest-1")
    monkeypatch.setenv("MODEL_ID", "global.moonshotai.kimi-k3")
    monkeypatch.setenv("LAUNCHPAD_MODEL_REGION", "us-west-2")
    monkeypatch.setenv("LAUNCHPAD_MODEL_CREDENTIAL", "launchpad-global-bedrock")
    path: Path = request.param
    spec = importlib.util.spec_from_file_location(f"byoc_sample_{path.parent.name}_{id(rec)}", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module, rec


def test_both_samples_share_one_model_path():
    """hello-container is hello-http in a Dockerfile — only the docstring title differs."""
    http, container = (p.read_text(encoding="utf-8").splitlines() for p in MAINS)
    assert http[1:] == container[1:]
    assert http[0] != container[0]


def test_workload_token_is_exchanged_for_the_key_and_the_bearer_set_before_the_client(sample):
    module, rec = sample
    rec.injected_token = "runtime-injected-token"

    out = module.invoke({"prompt": "Hello", "actor_id": "member"})

    assert out == {"result": "Hello from the model"}  # reasoning blocks are not the answer
    assert rec.api_key_calls == [
        {
            "workloadIdentityToken": "runtime-injected-token",
            "resourceCredentialProviderName": "launchpad-global-bedrock",
        }
    ]
    assert rec.identity_regions == ["cn-northwest-1"]  # Identity lives in the runtime's region
    assert rec.minted_workloads == []  # the injected token wins; no self-mint
    (client,) = rec.clients
    assert client["service"] == "bedrock-runtime"
    assert client["region_name"] == "us-west-2"  # the model region, never the runtime's
    assert client["bearer_at_init"] == "k-test-123"  # exported before construction
    assert client["config"].kwargs == {"signature_version": "bearer"}
    (converse,) = rec.converse_calls
    assert converse["modelId"] == "global.moonshotai.kimi-k3"
    assert converse["inferenceConfig"]["maxTokens"] >= 256
    assert converse["messages"] == [{"role": "user", "content": [{"text": "Hello"}]}]


def test_key_is_fetched_once_per_process(sample):
    module, rec = sample
    rec.injected_token = "tok"
    module.invoke({"prompt": "one"})
    module.invoke({"prompt": "two"})
    assert len(rec.api_key_calls) == 1
    assert len(rec.converse_calls) == 2


def test_self_mints_a_token_from_the_workload_name_when_none_is_injected(sample, monkeypatch):
    module, rec = sample
    rec.injected_token = None
    monkeypatch.setenv("LAUNCHPAD_WORKLOAD_NAME", "hello_http_abc123")

    out = module.invoke({"prompt": "Hi"})

    assert out == {"result": "Hello from the model"}
    assert rec.minted_workloads == ["hello_http_abc123"]
    assert rec.api_key_calls[0]["workloadIdentityToken"] == "minted-for-hello_http_abc123"


def test_echo_fallback_names_the_reason_when_no_token_is_available(sample):
    module, rec = sample
    rec.injected_token = None  # and no LAUNCHPAD_WORKLOAD_NAME

    out = module.invoke({"prompt": "Hello"})

    text = out["result"]
    assert text.startswith("[echo — model unavailable (global.moonshotai.kimi-k3 via launchpad-global-bedrock in us-west-2): ")
    assert "RuntimeError: no workload access token" in text
    assert text.endswith("] Hello")
    assert rec.clients == []  # never reached Bedrock
    assert BEARER_ENV not in os.environ


def test_model_error_echoes_and_drops_the_cached_key(sample):
    module, rec = sample
    rec.injected_token = "tok"
    rec.converse_error = RuntimeError("401 stale key")

    out = module.invoke({"prompt": "Hello"})
    assert "RuntimeError: 401 stale key" in out["result"]

    rec.converse_error = None
    assert module.invoke({"prompt": "again"}) == {"result": "Hello from the model"}
    assert len(rec.api_key_calls) == 2  # re-exchanged after the failure


def test_reasoning_only_answer_names_the_stop_reason_instead_of_returning_nothing(sample):
    """N17-obs114: GLM 5.3 spent the whole token budget on reasoning and returned no text
    block, so the sample answered ``{"result": ""}`` and the console showed a silent turn.
    The sample now names the stop reason (and a budget that leaves room for the text)."""
    module, rec = sample
    rec.injected_token = "tok"
    rec.converse_blocks = [{"reasoningContent": {"reasoningText": {"text": "thinking…" * 50}}}]
    rec.stop_reason = "max_tokens"

    out = module.invoke({"prompt": "A puzzle"})

    text = out["result"]
    assert text, "an empty result is a silent turn"
    assert text.startswith("[no answer — the model stopped (max_tokens) before producing text")
    assert f"MODEL_MAX_TOKENS={module.MODEL_MAX_TOKENS}" in text
    assert module.MODEL_MAX_TOKENS >= 4096
    (converse,) = rec.converse_calls
    assert converse["inferenceConfig"]["maxTokens"] == module.MODEL_MAX_TOKENS
