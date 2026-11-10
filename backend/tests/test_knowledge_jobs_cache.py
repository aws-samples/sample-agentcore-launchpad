"""N-SCN-obs11: the KB detail serves per-data-source ingestion jobs from a short
in-process cache (10 s TTL, single-flight per key) and still answers 200 with
`jobs_degraded: true` when the OpenSearch search fails or times out. The dedicated
ingestion-jobs route stays live and uncached."""

import threading

import pytest
import urllib3

from app.services import knowledge_backend, knowledge_selfhosted
from app.services import opensearch_http as core
from app.services.knowledge_selfhosted import (
    JOBS_CACHE_TTL_SECONDS,
    JOBS_SEARCH_TIMEOUT,
    JobsSearchTimedOut,
    SelfHostedKnowledgeBackend,
    cached_jobs,
    invalidate_jobs_cache,
)
from tests.conftest import set_default_resources, ws_ctx
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS
from tests.test_memory_selfhosted import SUBSTRATE, T0


class CountingOpenSearch(FakeOpenSearch):
    """Records every `search` body so a test can count lookups and inspect them."""

    def __init__(self):
        super().__init__()
        self.searches: list[tuple[str, dict]] = []
        self.fail_with: BaseException | None = None
        self.timed_out = False  # answer with `timed_out: true` and no hits (partial)

    def search(self, index, body):
        self.searches.append((index, body))
        if self.fail_with is not None:
            raise self.fail_with
        result = super().search(index, body)
        if self.timed_out:
            # what a busy node returns when the body `timeout` elapses before the shard
            # finished: a well-formed answer, `timed_out: true`, whatever hits it had
            return {**result, "timed_out": True, "hits": {**result["hits"], "hits": []}}
        return {**result, "timed_out": False}


@pytest.fixture
def clock(monkeypatch):
    """A controllable monotonic clock for the cache."""
    now = {"t": 1000.0}
    monkeypatch.setattr(knowledge_selfhosted, "_jobs_now", lambda: now["t"])
    return now


@pytest.fixture
def fakes():
    os_ = CountingOpenSearch()
    os_.ensure_index(core.jobs_index(""), core.jobs_index_body())
    os_.ensure_index(core.kb_docs_index(""), core.kb_docs_index_body())
    return {"s3": FakeS3(), "opensearch": os_, "sqs": FakeSQS(), "embedder": FakeEmbedder()}


@pytest.fixture
def backend(client, fakes):
    be = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    knowledge_backend.set_backend(be)
    set_default_resources(SUBSTRATE)
    return be


@pytest.fixture
def ws():
    return ws_ctx(SUBSTRATE, region="cn-northwest-1")


def _job_searches(fakes):
    return [b for (i, b) in fakes["opensearch"].searches if i == core.jobs_index("")]


def test_detail_polls_within_the_ttl_share_one_search(backend, ws, fakes, clock):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})  # create returns a detail
    assert len(_job_searches(fakes)) == 1
    for _ in range(5):
        detail = backend.get_kb_detail(ws, kb["kb_id"])
    assert len(_job_searches(fakes)) == 1  # five polls, zero extra searches
    assert detail["data_sources"][0]["ingestion_jobs"] == []
    assert detail["data_sources"][0]["jobs_degraded"] is False
    assert detail["jobs_degraded"] is False


def test_the_cache_expires_after_the_ttl(backend, ws, fakes, clock):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    clock["t"] += JOBS_CACHE_TTL_SECONDS - 0.5
    backend.get_kb_detail(ws, kb["kb_id"])
    assert len(_job_searches(fakes)) == 1
    clock["t"] += 1.0  # now past the TTL
    backend.get_kb_detail(ws, kb["kb_id"])
    assert len(_job_searches(fakes)) == 2
    assert JOBS_CACHE_TTL_SECONDS == 10.0


def test_the_jobs_search_carries_opensearch_s_own_timeout(backend, ws, fakes):
    backend.create_kb(ws, "docs", "", {"mode": "upload"})
    (body,) = _job_searches(fakes)
    assert body["timeout"] == JOBS_SEARCH_TIMEOUT == "5s"
    assert body["size"] == 5


def test_start_sync_invalidates_so_the_next_poll_sees_starting(backend, ws, fakes, clock):
    """A `[]` cached by the create's detail must not hide the job `start_sync` just
    seeded — the write drops the key."""
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds_id = kb["data_sources"][0]["ds_id"]
    out = backend.start_sync(ws, kb["kb_id"], ds_id)
    detail = backend.get_kb_detail(ws, kb["kb_id"])
    assert detail["data_sources"][0]["ingestion_jobs"][0]["job_id"] == out["job_id"]
    assert detail["data_sources"][0]["ingestion_jobs"][0]["status"] == "STARTING"


def test_delete_data_source_and_delete_kb_drop_their_keys(backend, ws, fakes):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = backend.add_data_source(
        ws, kb["kb_id"], {"mode": "existing", "bucket": SUBSTRATE["artifacts_bucket"],
                          "prefix": f"kb/{kb['kb_id']}/more/"},
    )
    backend.get_kb_detail(ws, kb["kb_id"])
    keys = {k for k in knowledge_selfhosted._JOBS_CACHE if k[1] == kb["kb_id"]}
    assert len(keys) == 2
    backend.delete_data_source(ws, kb["kb_id"], ds["ds_id"])
    assert {k for k in knowledge_selfhosted._JOBS_CACHE if k[1] == kb["kb_id"]} == {
        ("default", kb["kb_id"], kb["data_sources"][0]["ds_id"])
    }
    backend.delete_kb(ws, kb["kb_id"], force=True)
    assert not {k for k in knowledge_selfhosted._JOBS_CACHE if k[1] == kb["kb_id"]}


def test_a_timeout_degrades_the_detail_to_200_with_the_flag(client, backend, ws, fakes, clock):
    """The failure the field saw: urllib3 `ReadTimeoutError` surfacing raw from
    `OpenSearchHttp.request` after the retry budget. The detail answers 200 with `[]`
    and `jobs_degraded: true`; the degraded answer is cached for the TTL (one slow node
    answer costs one poll), then the next poll retries and recovers."""
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    clock["t"] += JOBS_CACHE_TTL_SECONDS + 1
    fakes["opensearch"].fail_with = urllib3.exceptions.ReadTimeoutError(
        None, "https://search-mem.example/launchpad-jobs/_search", "Read timed out."
    )
    res = client.get(f"/api/knowledge-bases/{kb['kb_id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["jobs_degraded"] is True
    assert body["data_sources"][0]["jobs_degraded"] is True
    assert body["data_sources"][0]["ingestion_jobs"] == []
    assert body["status"] == "ACTIVE"  # the rest of the detail is intact
    searches_after_failure = len(_job_searches(fakes))
    # within the TTL the degraded answer is served without touching the node again
    res = client.get(f"/api/knowledge-bases/{kb['kb_id']}")
    assert res.status_code == 200 and res.json()["jobs_degraded"] is True
    assert len(_job_searches(fakes)) == searches_after_failure
    # the node recovers → the next window re-queries and clears the flag
    fakes["opensearch"].fail_with = None
    clock["t"] += JOBS_CACHE_TTL_SECONDS + 1
    res = client.get(f"/api/knowledge-bases/{kb['kb_id']}")
    assert res.status_code == 200 and res.json()["jobs_degraded"] is False
    assert len(_job_searches(fakes)) == searches_after_failure + 1


def test_a_degraded_detail_keeps_the_last_known_jobs(backend, ws, fakes, clock, caplog):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds_id = kb["data_sources"][0]["ds_id"]
    out = backend.start_sync(ws, kb["kb_id"], ds_id)
    backend.get_kb_detail(ws, kb["kb_id"])  # caches the STARTING job
    clock["t"] += JOBS_CACHE_TTL_SECONDS + 1
    fakes["opensearch"].fail_with = core.OpenSearchError(503, "do not echo this body")
    with caplog.at_level("WARNING", logger="launchpad.knowledge"):
        detail = backend.get_kb_detail(ws, kb["kb_id"])
    ds = detail["data_sources"][0]
    assert ds["jobs_degraded"] is True
    assert [j["job_id"] for j in ds["ingestion_jobs"]] == [out["job_id"]]
    assert "jobs_degraded=true" in caplog.text and "OpenSearchError" in caplog.text
    # the dedicated route is live and uncached: it still raises → the router maps it
    with pytest.raises(core.OpenSearchError):
        backend.list_ingestion_jobs(ws, kb["kb_id"], ds_id)


def test_the_dedicated_jobs_route_is_uncached(backend, ws, fakes, clock):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds_id = kb["data_sources"][0]["ds_id"]
    before = len(_job_searches(fakes))
    backend.list_ingestion_jobs(ws, kb["kb_id"], ds_id)
    backend.list_ingestion_jobs(ws, kb["kb_id"], ds_id)
    assert len(_job_searches(fakes)) == before + 2


def test_concurrent_misses_are_single_flight(clock):
    """Twenty threads miss the same key at once; exactly one `build()` runs and every
    caller gets its result."""
    gate = threading.Event()
    builds: list[int] = []

    def build():
        builds.append(1)
        gate.wait(timeout=5)
        return [{"job_id": "ij1", "status": "IN_PROGRESS"}]

    results: list = []
    key = ("default", "KB1", "DS1")

    def poll():
        results.append(cached_jobs(key, build))

    threads = [threading.Thread(target=poll) for _ in range(20)]
    for t in threads:
        t.start()
    # let the first thread enter build() before releasing the gate
    for _ in range(200):
        if builds:
            break
        threading.Event().wait(0.005)
    gate.set()
    for t in threads:
        t.join(timeout=5)
    assert len(builds) == 1
    assert results == [([{"job_id": "ij1", "status": "IN_PROGRESS"}], False)] * 20


def test_keys_are_per_workspace_kb_and_data_source(clock):
    calls: list[str] = []
    for key in (("ws-a", "KB1", "DS1"), ("ws-b", "KB1", "DS1"), ("ws-a", "KB2", "DS1"),
                ("ws-a", "KB1", "DS2"), ("ws-a", "KB1", "DS1")):
        cached_jobs(key, lambda key=key: calls.append("|".join(key)) or [])
    assert calls == ["ws-a|KB1|DS1", "ws-b|KB1|DS1", "ws-a|KB2|DS1", "ws-a|KB1|DS2"]
    invalidate_jobs_cache("ws-a", "KB1")  # both DS keys of ws-a/KB1
    assert set(knowledge_selfhosted._JOBS_CACHE) == {("ws-b", "KB1", "DS1"),
                                                     ("ws-a", "KB2", "DS1")}


def test_expired_entries_are_evicted_on_store(clock):
    cached_jobs(("ws", "KB1", "DS1"), list)
    clock["t"] += JOBS_CACHE_TTL_SECONDS + 1
    cached_jobs(("ws", "KB2", "DS1"), list)
    assert set(knowledge_selfhosted._JOBS_CACHE) == {("ws", "KB2", "DS1")}


# ── r7-j review follow-ups ─────────────────────────────────────────────────────────


def test_invalidate_during_build_does_not_cache_the_stale_result(clock):
    """The race the r7-I review found: a poll's search runs *before* `start_sync` writes
    `STARTING`, `start_sync` invalidates, then the poll stores its pre-write `[]` as fresh
    — for 10 s the UI shows idle and the manual retry would start a second sync. With
    the per-key generation the racing build still answers its own caller (`[]` is what it
    saw) but is not stored; the next poll rebuilds and sees `STARTING`."""
    key = ("default", "KB1", "DS1")
    builds: list[str] = []

    def racing_build():
        builds.append("pre-write")
        invalidate_jobs_cache("default", "KB1", "DS1")  # start_sync lands mid-build
        return []  # what the search saw before the write

    assert cached_jobs(key, racing_build) == ([], False)  # the caller gets its answer
    assert key not in knowledge_selfhosted._JOBS_CACHE  # ...but nothing was stored

    def post_write_build():
        builds.append("post-write")
        return [{"job_id": "ij1", "status": "STARTING"}]

    jobs, degraded = cached_jobs(key, post_write_build)
    assert (jobs, degraded) == ([{"job_id": "ij1", "status": "STARTING"}], False)
    assert builds == ["pre-write", "post-write"]
    # and now it *is* cached: a further poll within the TTL does not build
    assert cached_jobs(key, lambda: builds.append("third") or []) == (jobs, False)
    assert builds == ["pre-write", "post-write"]


def test_invalidate_during_a_failing_build_does_not_cache_the_degraded_answer(clock):
    key = ("default", "KB1", "DS1")

    def failing_build():
        invalidate_jobs_cache("default", "KB1")
        raise core.OpenSearchError(503, "busy")

    assert cached_jobs(key, failing_build) == ([], True)
    assert key not in knowledge_selfhosted._JOBS_CACHE


def test_invalidate_bumps_the_generation_of_a_key_that_was_never_stored(clock):
    """The first build of a key has nothing in `_JOBS_CACHE` to pop; the generation is
    registered before the build starts so the invalidation still reaches it."""
    key = ("default", "KB1", "DS1")
    assert key not in knowledge_selfhosted._JOBS_GEN
    cached_jobs(key, lambda: invalidate_jobs_cache("default", "KB1") or [])
    assert knowledge_selfhosted._JOBS_GEN[key] == 1
    assert key not in knowledge_selfhosted._JOBS_CACHE


def test_a_whole_kb_invalidation_reaches_every_data_source_in_flight(clock):
    seen: list[tuple] = []
    for ds in ("DS1", "DS2"):
        key = ("default", "KB1", ds)
        cached_jobs(key, lambda: [])  # both cached
    invalidate_jobs_cache("default", "KB1")  # ds_id=None
    assert not {k for k in knowledge_selfhosted._JOBS_CACHE if k[1] == "KB1"}
    for ds in ("DS1", "DS2"):
        cached_jobs(("default", "KB1", ds), lambda ds=ds: seen.append(ds) or [])
    assert seen == ["DS1", "DS2"]  # both rebuilt, both cached again
    assert len({k for k in knowledge_selfhosted._JOBS_CACHE if k[1] == "KB1"}) == 2


def test_an_opensearch_timed_out_answer_is_degraded_not_fresh(backend, ws, fakes, clock, caplog):
    """`timed_out: true` means OpenSearch gave up at `JOBS_SEARCH_TIMEOUT` and returned
    the hits it had (often none) — a well-formed answer that used to be cached as the
    truth for a TTL. The detail now keeps the last complete list and sets the flag."""
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds_id = kb["data_sources"][0]["ds_id"]
    out = backend.start_sync(ws, kb["kb_id"], ds_id)
    backend.get_kb_detail(ws, kb["kb_id"])  # caches the complete list: [STARTING]
    clock["t"] += JOBS_CACHE_TTL_SECONDS + 1
    fakes["opensearch"].timed_out = True
    with caplog.at_level("WARNING", logger="launchpad.knowledge"):
        detail = backend.get_kb_detail(ws, kb["kb_id"])
    ds = detail["data_sources"][0]
    assert ds["jobs_degraded"] is True and detail["jobs_degraded"] is True
    assert [j["job_id"] for j in ds["ingestion_jobs"]] == [out["job_id"]]  # not the partial []
    assert "JobsSearchTimedOut" in caplog.text and "jobs_degraded=true" in caplog.text
    # the degraded answer is cached for the TTL like any other failure
    searches = len(_job_searches(fakes))
    assert backend.get_kb_detail(ws, kb["kb_id"])["jobs_degraded"] is True
    assert len(_job_searches(fakes)) == searches
    # the live route keeps the partial hits (no flag to carry them on) and does not raise
    assert backend.list_ingestion_jobs(ws, kb["kb_id"], ds_id) == []
    # the node finishes its searches again → the next window clears the flag
    fakes["opensearch"].timed_out = False
    clock["t"] += JOBS_CACHE_TTL_SECONDS + 1
    detail = backend.get_kb_detail(ws, kb["kb_id"])
    assert detail["jobs_degraded"] is False
    assert [j["job_id"] for j in detail["data_sources"][0]["ingestion_jobs"]] == [out["job_id"]]


def test_jobs_search_timed_out_is_raised_only_for_the_cached_path(backend, ws, fakes):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds_id = kb["data_sources"][0]["ds_id"]
    fakes["opensearch"].timed_out = True
    with pytest.raises(JobsSearchTimedOut) as info:
        backend._jobs(ws, kb["kb_id"], ds_id, size=5, partial_ok=False)
    assert "timed out after 5s" in str(info.value) and "0 partial hit(s)" in str(info.value)
    assert backend._jobs(ws, kb["kb_id"], ds_id, size=5) == []  # default: partial is fine
