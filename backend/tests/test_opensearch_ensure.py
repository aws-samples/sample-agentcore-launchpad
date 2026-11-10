"""WB-007: `opensearch_ensure.ensure_index` closes the HEAD → PUT create race hub-side.

The core's `ensure_index` is mirrored into the extractor Lambda and the runtime template
(ruling R15) and neither of them creates indices, so the fix wraps the core instead of
changing it; `tests/test_memory_core_mirror.py` keeps pinning the three copies."""

import copy
import json
import threading
from types import SimpleNamespace

import pytest
from botocore.credentials import Credentials

from app.core import naming
from app.services import memory_bootstrap as mb
from app.services import opensearch_ensure
from app.services import opensearch_http as core
from app.services.knowledge_selfhosted import SelfHostedKnowledgeBackend
from tests.conftest import ws_ctx
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS
from tests.test_memory_bootstrap import SecOS
from tests.test_memory_selfhosted import SUBSTRATE, T0

CREDS = Credentials("AKIATEST", "secret", "token")
EXISTS_BODY = json.dumps({
    "error": {
        "root_cause": [{"type": "resource_already_exists_exception",
                        "reason": "index [launchpad-kb-x/abc] already exists"}],
        "type": "resource_already_exists_exception",
        "reason": "index [launchpad-kb-x/abc] already exists",
    },
    "status": 400,
}).encode()


def _live(body: dict, name: str = "idx") -> dict:
    """What `GET /<index>` returns for an index created from `body`: the same
    properties, `settings.index.*` stringified the way OpenSearch answers."""
    settings = {k.split(".", 1)[1] if k.startswith("index.") else k: str(v).lower()
                for k, v in body["settings"].items()}
    return {name: {"aliases": {}, "mappings": copy.deepcopy(body["mappings"]),
                   "settings": {"index": {**settings, "uuid": "abc", "provided_name": name}}}}


class _RacePool:
    """urllib3-shaped pool simulating the loser of a create race: HEAD says missing (stale),
    PUT answers 400 already-exists, GET returns `live`."""

    def __init__(self, live: dict, get_status: int = 200) -> None:
        self.live, self.get_status = live, get_status
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, body=None, headers=None):
        path = url.split("/", 3)[-1]
        self.calls.append((method, path))
        if method == "HEAD":
            return SimpleNamespace(status=404, data=b"")
        if method == "PUT":
            return SimpleNamespace(status=400, data=EXISTS_BODY)
        if method == "GET":
            return SimpleNamespace(status=self.get_status, data=json.dumps(self.live).encode())
        raise AssertionError(method)


def _client(pool) -> core.OpenSearchHttp:
    return core.OpenSearchHttp("search-x.cn-northwest-1.es.example", "cn-northwest-1",
                               lambda: CREDS, http=pool)


# ── compatibility rule ───────────────────────────────────────────────────────────
def test_identical_live_mapping_is_compatible_even_with_extra_dynamic_fields():
    body = core.kb_index_body()
    live = _live(body)["idx"]
    live["mappings"]["properties"]["extra_dynamic"] = {"type": "text"}
    assert opensearch_ensure.index_compatibility_problems(body, live) == []


@pytest.mark.parametrize("mutate, needle", [
    (lambda p: p["embedding"].__setitem__("dimension", 512), "dimension 512 != 1024"),
    (lambda p: p["embedding"]["method"].__setitem__("engine", "nmslib"),
     "method.engine 'nmslib' != 'lucene'"),
    (lambda p: p["embedding"]["method"].__setitem__("space_type", "l2"),
     "method.space_type 'l2' != 'cosinesimil'"),
    (lambda p: p["embedding"]["method"].__setitem__("name", "ivf"), "method.name 'ivf'"),
    (lambda p: p.pop("kb_id"), "field 'kb_id' missing"),
    (lambda p: p["kb_id"].__setitem__("type", "text"), "field 'kb_id' type 'text' != 'keyword'"),
    (lambda p: p["metadata"].__setitem__("enabled", True), "field 'metadata' enabled True"),
])
def test_conflicting_live_mappings_are_incompatible(mutate, needle):
    body = core.kb_index_body()
    live = _live(body)["idx"]
    mutate(live["mappings"]["properties"])
    problems = opensearch_ensure.index_compatibility_problems(body, live)
    assert len(problems) == 1 and needle in problems[0]


def test_a_live_index_without_knn_enabled_is_incompatible_for_a_vector_body():
    body = core.kb_index_body()
    live = _live(body)["idx"]
    live["settings"]["index"]["knn"] = "false"
    assert opensearch_ensure.index_compatibility_problems(body, live) == [
        "settings index.knn 'false' != True"
    ]
    # shard/replica counts are capacity, never a conflict
    live["settings"]["index"].update(knn="true", number_of_shards="5", number_of_replicas="2")
    assert opensearch_ensure.index_compatibility_problems(body, live) == []


def test_a_body_without_vectors_ignores_knn_but_still_checks_types():
    body = core.jobs_index_body()
    live = _live(body)["idx"]
    live["settings"]["index"]["knn"] = "false"  # jobs index asked for knn: True → conflict
    assert opensearch_ensure.index_compatibility_problems(body, live) == [
        "settings index.knn 'false' != True"
    ]
    body["settings"] = {"number_of_shards": 1}
    assert opensearch_ensure.index_compatibility_problems(body, live) == []


# ── the race against the real OpenSearchHttp ─────────────────────────────────────
def test_stale_head_then_put_exists_is_ensured_not_created_when_compatible():
    body = core.kb_index_body()
    pool = _RacePool(_live(body))
    assert opensearch_ensure.ensure_index(_client(pool), "idx", body) is False
    assert [m for m, _ in pool.calls] == ["HEAD", "PUT", "GET"]
    assert pool.calls[-1] == ("GET", "idx")


def test_stale_head_then_put_exists_raises_index_incompatible_when_it_conflicts():
    body = core.kb_index_body()
    live = _live(body)
    live["idx"]["mappings"]["properties"]["embedding"]["dimension"] = 256
    with pytest.raises(opensearch_ensure.IndexIncompatible) as exc:
        opensearch_ensure.ensure_index(_client(_RacePool(live)), "idx", body)
    assert isinstance(exc.value, core.OpenSearchError) and exc.value.status == 409
    assert exc.value.name == "idx" and exc.value.problems == [
        "field 'embedding' dimension 256 != 1024"
    ]
    assert "incompatible mapping" in str(exc.value) and "dimension 256" in str(exc.value)


def test_alias_lookup_falls_back_to_the_single_backing_index():
    body = core.kb_index_body()
    pool = _RacePool(_live(body, name="idx-000001"))
    assert opensearch_ensure.ensure_index(_client(pool), "idx", body) is False


def test_other_400s_and_the_get_failing_propagate_unchanged():
    body = core.kb_index_body()

    class _OtherPool(_RacePool):
        def request(self, method, url, body=None, headers=None):
            if method == "PUT":
                return SimpleNamespace(status=400, data=b'{"error":{"type":"mapper_parsing"}}')
            return super().request(method, url, body, headers)

    with pytest.raises(core.OpenSearchError) as exc:
        opensearch_ensure.ensure_index(_client(_OtherPool({})), "idx", body)
    assert exc.value.status == 400
    assert not isinstance(exc.value, opensearch_ensure.IndexIncompatible)
    # the follow-up GET failing (e.g. the index vanished again) is not swallowed either
    with pytest.raises(core.OpenSearchError) as exc:
        opensearch_ensure.ensure_index(_client(_RacePool({}, get_status=404)), "idx", body)
    assert exc.value.status == 404


def test_happy_path_creates_and_a_found_head_short_circuits():
    class _Pool:
        def __init__(self):
            self.exists = False
            self.calls = []

        def request(self, method, url, body=None, headers=None):
            self.calls.append(method)
            if method == "HEAD":
                return SimpleNamespace(status=200 if self.exists else 404, data=b"")
            self.exists = True
            return SimpleNamespace(status=200, data=b'{"acknowledged":true}')

    pool = _Pool()
    assert opensearch_ensure.ensure_index(_client(pool), "idx", core.kb_index_body()) is True
    assert opensearch_ensure.ensure_index(_client(pool), "idx", core.kb_index_body()) is False
    assert pool.calls == ["HEAD", "PUT", "HEAD"]


def test_concurrent_creators_end_with_one_index_and_no_error():
    """Eight threads race the same index on a pool whose HEAD is always stale (404) and
    whose PUT is atomic: exactly one creates, the rest are ensured, nobody raises."""
    body = core.kb_index_body()
    lock = threading.Lock()
    state = {"created": 0}

    class _AtomicPool:
        def request(self, method, url, body=None, headers=None):
            if method == "HEAD":
                return SimpleNamespace(status=404, data=b"")
            if method == "PUT":
                with lock:
                    if state["created"]:
                        return SimpleNamespace(status=400, data=EXISTS_BODY)
                    state["created"] += 1
                    return SimpleNamespace(status=200, data=b'{"acknowledged":true}')
            return SimpleNamespace(
                status=200, data=json.dumps(_live(core.kb_index_body())).encode()
            )

    client = _client(_AtomicPool())
    results: list = []
    errors: list = []

    def worker():
        try:
            results.append(opensearch_ensure.ensure_index(client, "idx", body))
        except Exception as exc:  # noqa: BLE001 — the assertion below reports it
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and state["created"] == 1
    assert sorted(results) == [False] * 7 + [True]


# ── the callers ──────────────────────────────────────────────────────────────────
def _racing(fake: FakeOpenSearch, live_body: dict) -> None:
    """Make a FakeOpenSearch behave like the loser of the race: `ensure_index` raises the
    400 already-exists the core would, `request("GET", /<index>)` answers `live_body`."""
    def lose(name, body):
        fake.indices.setdefault(name, {})
        raise core.OpenSearchError(400, EXISTS_BODY.decode())

    def request(method, path, body=None, params=None):
        assert method == "GET"
        return _live(live_body, name=path.lstrip("/"))

    fake.ensure_index = lose
    fake.request = request


def test_create_kb_losing_the_race_is_active_not_failed(client):
    fakes = {"s3": FakeS3(), "opensearch": FakeOpenSearch(), "sqs": FakeSQS(),
             "embedder": FakeEmbedder()}
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    _racing(fakes["opensearch"], core.kb_index_body())
    kb = backend.create_kb(ws, "raced", "", {"mode": "upload"})
    assert kb["status"] == "ACTIVE" and kb["failure_reasons"] == []


def test_create_kb_losing_the_race_to_an_incompatible_index_is_failed_with_the_reason(client):
    fakes = {"s3": FakeS3(), "opensearch": FakeOpenSearch(), "sqs": FakeSQS(),
             "embedder": FakeEmbedder()}
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    wrong = core.kb_index_body()
    wrong["mappings"]["properties"]["embedding"]["dimension"] = 384
    _racing(fakes["opensearch"], wrong)
    kb = backend.create_kb(ws, "raced-wrong", "", {"mode": "upload"})
    assert kb["status"] == "FAILED"
    assert "incompatible mapping" in kb["failure_reasons"][0]
    assert "dimension 384 != 1024" in kb["failure_reasons"][0]


def test_ensure_memory_substrate_treats_a_raced_index_as_reused():
    os_ = SecOS()
    inner_request = os_.request
    seen = []

    def lose(name, body):
        seen.append(name)
        raise core.OpenSearchError(400, EXISTS_BODY.decode())

    def request(method, path, body=None, params=None):
        if method == "GET" and not path.startswith("/_plugins/"):
            # `GET /<index>` (race check) and, r14-a, `GET /<index>/_mapping` (additive check)
            name = path.lstrip("/").removesuffix("/_mapping")
            live = dict(mb.fixed_indices())[name]
            return _live(live, name=name)
        return inner_request(method, path, body, params)

    os_.ensure_index = lose
    os_.request = request
    out = mb.ensure_memory_substrate(
        os_, exec_role_arn="arn:aws-cn:iam::111:role/exec",
        extractor_role_arn="arn:aws-cn:iam::111:role/extractor",
        agent_role_pattern="arn:aws-cn:iam::111:role/launchpad-agent-*",
    )
    names = [naming.memory_index(), naming.jobs_index(), naming.kb_docs_index()]
    assert seen == names
    assert [f"index {n}" for n in names] == [c for c in out["reused"] if c.startswith("index")]
    assert not [c for c in out["created"] if c.startswith("index")]


def test_ensure_memory_substrate_fails_the_stage_on_an_incompatible_raced_index():
    os_ = SecOS()

    def lose(name, body):
        raise core.OpenSearchError(400, EXISTS_BODY.decode())

    def request(method, path, body=None, params=None):
        name = path.lstrip("/")
        live = dict(mb.fixed_indices())[name]
        wrong = _live(live, name=name)
        wrong[name]["mappings"]["properties"].pop("store_id", None)
        wrong[name]["mappings"]["properties"].pop("job_id", None)
        return wrong

    os_.ensure_index = lose
    os_.request = request
    with pytest.raises(opensearch_ensure.IndexIncompatible) as exc:
        mb.ensure_memory_substrate(
            os_, exec_role_arn="arn:aws-cn:iam::111:role/exec",
            extractor_role_arn="arn:aws-cn:iam::111:role/extractor",
            agent_role_pattern="arn:aws-cn:iam::111:role/launchpad-agent-*",
        )
    assert exc.value.name == naming.memory_index()
    assert exc.value.problems == ["field 'store_id' missing"]
