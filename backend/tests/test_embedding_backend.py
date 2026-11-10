"""D21 (M1-d): hub-side embeddings follow the registry's active embedding row."""

import json

import httpx
import pytest

from app.schemas.agent import active_embedding, catalog_version
from app.services import aws_clients
from app.services import embedding_backend as eb
from app.services import opensearch_http as core


def _transport(dimension=1024, status=200, body=None):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if body is not None:
            return httpx.Response(status, json=body)
        payload = json.loads(request.content)
        return httpx.Response(status, json={
            "data": [{"embedding": [0.5] * dimension, "index": 0}],
            "model": payload["model"], "usage": {"prompt_tokens": 1},
        })

    return httpx.MockTransport(handler), seen


def test_openai_embedder_posts_to_embeddings_with_the_bearer_and_checks_the_dimension():
    transport, seen = _transport(dimension=4)
    emb = eb.OpenAIEmbedder(base_url="https://api.siliconflow.cn/v1/", api_key="sk-secret",
                            api_model_name="BAAI/bge-m3", dimension=4, max_chars=5,
                            transport=transport)
    assert emb.embed("hello world") == [0.5] * 4
    req = seen[0]
    assert str(req.url) == "https://api.siliconflow.cn/v1/embeddings"
    assert req.headers["Authorization"] == "Bearer sk-secret"
    assert json.loads(req.content) == {"model": "BAAI/bge-m3", "input": "hello"}  # max_chars cut
    assert emb.model_id == "BAAI/bge-m3" and emb.dimensions == 4


def test_openai_embedder_refuses_a_vector_of_another_dimension():
    transport, _ = _transport(dimension=768)
    emb = eb.OpenAIEmbedder(base_url="https://x/v1", api_key="k", api_model_name="m",
                            dimension=1024, transport=transport)
    with pytest.raises(eb.EmbeddingDimensionMismatch, match="768-dimensional.*declares 1024"):
        emb.embed("x")


def test_openai_embedder_errors_never_echo_the_key():
    transport, _ = _transport(status=401, body={"error": "bad key"})
    emb = eb.OpenAIEmbedder(base_url="https://x/v1", api_key="sk-very-secret", api_model_name="m",
                            transport=transport)
    with pytest.raises(RuntimeError, match="answered 401") as exc:
        emb.embed("x")
    assert "sk-very-secret" not in str(exc.value)
    transport, _ = _transport(body={"data": []})
    with pytest.raises(RuntimeError, match="no data\\[0\\].embedding"):
        eb.OpenAIEmbedder(base_url="https://x/v1", api_key="k", api_model_name="m",
                          transport=transport).embed("x")


def test_message_block_for_the_seed_is_titan_1024():
    row = active_embedding()
    assert row is not None and row.model_id == core.EMBED_MODEL_ID
    block = eb.message_block()
    assert block == {
        "model_id": core.EMBED_MODEL_ID, "kind": "bedrock", "base_url": None,
        "api_model_name": core.EMBED_MODEL_ID, "credential_provider": "launchpad-global-bedrock",
        "dimension": core.EMBED_DIMENSIONS, "max_chars": core.EMBED_MAX_CHARS,
        "provider": "bedrock", "model_region": "us-west-2", "catalog_version": catalog_version(),
    }
    assert json.loads(json.dumps(block)) == block
    assert eb.embedding_dimension() == core.EMBED_DIMENSIONS


def test_vector_index_body_rewrites_only_knn_dimensions():
    memory = eb.vector_index_body(core.memory_index_body(), 768)
    assert memory["mappings"]["properties"]["embedding"]["dimension"] == 768
    assert core.memory_index_body()["mappings"]["properties"]["embedding"]["dimension"] == 1024
    kb = eb.vector_index_body(core.kb_index_body(), 384)
    assert kb["mappings"]["properties"]["embedding"]["dimension"] == 384
    # the seed → identical bodies (the bootstrap's PUT-when-different sees no drift)
    assert eb.vector_index_body(core.memory_index_body()) == core.memory_index_body()
    assert eb.vector_index_body(core.jobs_index_body()) == core.jobs_index_body()


def test_build_embedder_picks_titan_for_the_seed_and_openai_for_a_compatible_row():
    fake_bedrock = object()
    titan = eb.build_embedder(bedrock_runtime=fake_bedrock)
    assert isinstance(titan, core.TitanEmbedder)
    assert titan.bedrock_runtime is fake_bedrock and titan.model_id == core.EMBED_MODEL_ID
    assert titan.dimensions == core.EMBED_DIMENSIONS
    transport, seen = _transport(dimension=8)
    openai = eb.build_embedder(
        {"model_id": "siliconflow.BAAI/bge-m3", "kind": "openai_compatible",
         "base_url": "https://api.siliconflow.cn/v1", "api_model_name": "BAAI/bge-m3",
         "credential_provider": "launchpad-siliconflow", "dimension": 8, "max_chars": 100,
         "provider": "siliconflow"},
        api_key="injected", transport=transport)
    assert isinstance(openai, eb.OpenAIEmbedder) and openai.embed("x") == [0.5] * 8
    assert seen[0].headers["Authorization"] == "Bearer injected"


def test_provider_api_key_shim_prefers_m1b_reader_and_falls_back(monkeypatch):
    monkeypatch.setattr(aws_clients, "provider_api_key", lambda name: f"ssm:{name}", raising=False)
    assert eb.provider_api_key("siliconflow") == "ssm:siliconflow"
    monkeypatch.delattr(aws_clients, "provider_api_key", raising=False)
    monkeypatch.setattr(aws_clients, "require_provider_api_key", lambda name: f"env:{name}")
    assert eb.provider_api_key("siliconflow") == "env:siliconflow"


def test_build_embedder_resolves_the_providers_key_through_the_shim(monkeypatch):
    monkeypatch.setattr(eb, "provider_api_key", lambda name: f"key-of-{name}")
    transport, seen = _transport(dimension=2)
    emb = eb.build_embedder({"kind": "openai_compatible", "base_url": "https://x/v1",
                             "api_model_name": "m", "provider": "acme", "dimension": 2},
                            transport=transport)
    emb.embed("t")
    assert seen[0].headers["Authorization"] == "Bearer key-of-acme"
