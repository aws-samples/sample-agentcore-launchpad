"""The shared OpenSearch/Titan core (N6). Hermetic: SigV4 signs against static test
credentials; the HTTP pool is a stub that records what would have been sent."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.credentials import Credentials

from app.services import aws_clients
from app.services import opensearch_http as core
from app.services.workspace import WorkspaceContext
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch

CREDS = Credentials("AKIATEST", "secret", "token")


class _Pool:
    def __init__(self, status=200, body=b"{}"):
        self.calls: list[dict] = []
        self.status, self.body = status, body

    def request(self, method, url, body=None, headers=None):
        self.calls.append(
            {"method": method, "url": url, "body": body, "headers": dict(headers or {})}
        )
        return SimpleNamespace(status=self.status, data=self.body)


def _client(pool) -> core.OpenSearchHttp:
    return core.OpenSearchHttp("search-lp-memory-abc.cn-northwest-1.es.example", "cn-northwest-1",
                               lambda: CREDS, http=pool)


def test_requests_are_sigv4_signed_for_the_es_service():
    pool = _Pool(body=b'{"acknowledged": true}')
    out = _client(pool).request("PUT", "/launchpad-memory", {"settings": {"index.knn": True}})
    assert out == {"acknowledged": True}
    (call,) = pool.calls
    assert call["url"] == "https://search-lp-memory-abc.cn-northwest-1.es.example/launchpad-memory"
    assert call["headers"]["Content-Type"] == "application/json"
    assert "Credential=AKIATEST/" in call["headers"]["Authorization"]
    assert "/cn-northwest-1/es/aws4_request" in call["headers"]["Authorization"]
    assert call["headers"]["X-Amz-Security-Token"] == "token"
    assert json.loads(call["body"]) == {"settings": {"index.knn": True}}


def test_non_2xx_raises_with_the_body():
    pool = _Pool(status=403, body=b'{"error": "no permissions"}')
    with pytest.raises(core.OpenSearchError) as exc:
        _client(pool).request("GET", "/launchpad-memory/_search")
    assert exc.value.status == 403 and "no permissions" in str(exc.value)


def test_index_exists_uses_head_and_ensure_index_is_idempotent():
    pool = _Pool(status=404)
    client = _client(pool)
    assert client.index_exists("launchpad-memory") is False
    pool.status, pool.body = 200, b'{"acknowledged": true}'
    # HEAD 200 → reused
    assert client.ensure_index("launchpad-memory", core.memory_index_body()) is False
    assert [c["method"] for c in pool.calls] == ["HEAD", "HEAD"]


def test_memory_index_mapping_is_knn_cosine_1024():
    body = core.memory_index_body()
    assert body["settings"]["index.knn"] is True
    emb = body["mappings"]["properties"]["embedding"]
    assert emb == {"type": "knn_vector", "dimension": 1024,
                   "method": {"name": "hnsw", "space_type": "cosinesimil", "engine": "lucene"}}
    for field in ("store_id", "workspace_id", "actor_id", "namespace", "strategy", "session_id"):
        assert body["mappings"]["properties"][field] == {"type": "keyword"}
    assert body["mappings"]["properties"]["text"] == {"type": "text"}
    assert body["mappings"]["properties"]["created_at"] == {"type": "date"}
    assert core.kb_index_body()["mappings"]["properties"]["embedding"]["dimension"] == 1024
    assert core.kb_index_name("KB1a2B", "") == "launchpad-kb-kb1a2b"
    assert core.kb_index_name("KB1a2B", "t1") == "launchpad-t1-kb-kb1a2b"


def test_knn_search_builds_a_filtered_knn_query_and_strips_embeddings():
    hits = {"hits": {"hits": [
        {"_id": "r1", "_score": 0.9, "_source": {"text": "likes tea", "embedding": [0.1]}}
    ]}}
    pool = _Pool(body=json.dumps(hits).encode())
    out = _client(pool).knn_search(
        "launchpad-memory", [0.1, 0.2], 3, [{"term": {"namespace": "/facts/a__b"}}]
    )
    sent = json.loads(pool.calls[0]["body"])
    assert sent["size"] == 3 and "embedding" in sent["_source"]["excludes"]
    knn = sent["query"]["knn"]["embedding"]
    assert knn["vector"] == [0.1, 0.2] and knn["k"] == 3
    assert knn["filter"] == {"bool": {"filter": [{"term": {"namespace": "/facts/a__b"}}]}}
    assert out == [{"_id": "r1", "_score": 0.9, "text": "likes tea", "embedding": [0.1]}]


def test_titan_embedder_sends_the_v2_body_and_truncates():
    calls = []

    class Runtime:
        def invoke_model(self, **kw):
            import io
            calls.append(kw)
            return {"body": io.BytesIO(json.dumps({"embedding": [0.5, 0.5]}).encode())}

    assert core.TitanEmbedder(Runtime()).embed("x" * 9000) == [0.5, 0.5]
    (call,) = calls
    assert call["modelId"] == "amazon.titan-embed-text-v2:0"
    assert call["contentType"] == "application/json"
    assert json.loads(call["body"]) == {
        "inputText": "x" * 8000, "dimensions": 1024, "normalize": True
    }


def test_chunk_text_respects_size_and_overlap():
    text = "\n\n".join(f"para {i} " + "word " * 120 for i in range(6))
    chunks = core.chunk_text(text, size=1000, overlap=200)
    assert all(len(c) <= 1000 for c in chunks) and len(chunks) >= 4
    assert chunks[1].startswith(chunks[0][-200:].lstrip()[:20])   # overlap carried forward
    assert core.chunk_text("", size=1000, overlap=200) == []
    assert core.chunk_text("short", size=1000, overlap=200) == ["short"]


def test_cosine_and_namespace_resolution():
    assert core.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert core.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert core.resolve_namespace("/facts/{actorId}", "a__b") == ("/facts/a__b", True, False)
    assert core.resolve_namespace(
        "/summaries/{actorId}/{sessionId}", "a__b"
    ) == ("/summaries/a__b/", True, True)
    assert core.resolve_namespace(
        "/summaries/{actorId}/{sessionId}", "a__b", "s1"
    ) == ("/summaries/a__b/s1", True, False)
    assert core.resolve_namespace(
        "/x/{teamId}/{actorId}", "a__b"
    ) == ("/x/{teamId}/a__b", False, False)
    assert [s["strategy_id"] for s in core.STRATEGIES] == [
        "semantic", "user_preference", "summarization"
    ]
    # R13 (N13-obs4): every catalogued strategy is extracted — no reserved entries
    assert core.EXTRACTED_STRATEGIES == tuple(s["strategy_id"] for s in core.STRATEGIES)


def test_turn_event_is_agentcore_shaped_and_its_key_sorts_by_time():
    ts = datetime(2026, 10, 9, 12, 0, 0, 123456, tzinfo=UTC)
    event = core.turn_event("agent__river", "sess-1", "hi", "hello", ts)
    assert event["actorId"] == "agent__river" and event["sessionId"] == "sess-1"
    assert event["eventTimestamp"] == "2026-10-09T12:00:00.123456+00:00"
    assert [p["conversational"]["role"] for p in event["payload"]] == ["USER", "ASSISTANT"]
    assert event["payload"][1]["conversational"]["content"]["text"] == "hello"
    key = core.event_key(ts, event["eventId"][:8])
    assert key.startswith("20261009T120000123456Z_") and key.endswith(".json")
    assert core.parse_event_key(key) == ts and core.parse_event_key("garbage.json") is None


def test_fakes_round_trip_so_later_suites_can_trust_them():
    fake, emb = FakeOpenSearch(), FakeEmbedder()
    assert fake.ensure_index("launchpad-memory", core.memory_index_body()) is True
    assert fake.ensure_index("launchpad-memory", core.memory_index_body()) is False
    fake.put_doc("launchpad-memory", "1", {
        "namespace": "/facts/a", "text": "tea", "embedding": emb.embed("tea"),
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    fake.put_doc("launchpad-memory", "2", {
        "namespace": "/facts/b", "text": "coffee", "embedding": emb.embed("coffee"),
        "created_at": "2026-01-02T00:00:00+00:00",
    })
    hits = fake.search("launchpad-memory", {
        "query": {"bool": {"filter": [{"prefix": {"namespace": "/facts/"}}]}},
        "sort": [{"created_at": {"order": "desc"}}], "from": 0, "size": 10,
    })
    assert [h["_id"] for h in hits["hits"]["hits"]] == ["2", "1"]
    assert fake.knn_search(
        "launchpad-memory", emb.embed("tea"), 1, [{"prefix": {"namespace": "/facts/"}}]
    )[0]["_id"] == "1"
    assert fake.delete_by_query("launchpad-memory", {"term": {"namespace": "/facts/a"}}) == 1
    assert core.cosine(emb.embed("tea"), emb.embed("tea")) == pytest.approx(1.0)


def _embed_in_a_fresh_process(text: str, hash_seed: str) -> list[float]:
    """`FakeEmbedder.embed(text)` from a brand-new interpreter pinned to
    `hash_seed` — used to prove the vector no longer depends on Python's
    per-process string-hash salt (unset `PYTHONHASHSEED` in this repo)."""
    backend_dir = Path(__file__).resolve().parents[1]
    script = (
        "import json; from tests.memory_fakes import FakeEmbedder; "
        f"print(json.dumps(FakeEmbedder().embed({text!r})))"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        cwd=backend_dir,
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)


def test_fake_embedder_is_stable_across_process_hash_seeds():
    """Regression (N6 Task 2 fix round 1): the previous implementation bucketed
    tokens with the built-in `hash()`, which is randomly salted per process —
    `test_search_and_retrieve_use_knn_over_the_namespace` flaked ~15-25% of the
    time because two unrelated runs could rank the same two documents
    differently. `embed()` must now be byte-for-byte identical regardless of
    `PYTHONHASHSEED`."""
    text = "User likes green tea in the morning"
    seed_0 = _embed_in_a_fresh_process(text, "0")
    seed_other = _embed_in_a_fresh_process(text, "4294967295")
    assert seed_0 == seed_other
    assert seed_0 == FakeEmbedder().embed(text)


def test_funnel_factory_caches_per_target_and_endpoint(monkeypatch):
    aws_clients.reset_cache()
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: SimpleNamespace(
        get_credentials=lambda: SimpleNamespace(get_frozen_credentials=lambda: CREDS)))
    ctx = WorkspaceContext(account_id="111122223333", region="cn-northwest-1")
    a = aws_clients.opensearch_http(ctx, "search-a.example")
    assert aws_clients.opensearch_http(ctx, "search-a.example") is a
    assert aws_clients.opensearch_http(ctx, "search-b.example") is not a
    assert isinstance(a, core.OpenSearchHttp) and a.credentials_provider() is CREDS
    aws_clients.reset_cache()
    assert aws_clients._OPENSEARCH_HTTP == {}
