"""Long-term memory merge strategy v2 (r14-a, N14-obs1) — hermetic (C8).

Extractor operation intent (add / update / supersede / delete / noop, gray zone, dedupe),
bounded consolidation (per-actor caps), soft TTL, rolling actor summary, recall ranking on
the hub and in the rendered runtime library, mapping additivity on a legacy index, and the
store-settings API. Stub model + stub OpenSearch from `tests/memory_fakes.py`.
"""

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.services import memory_backend, opensearch_ensure
from app.services import opensearch_http as core
from app.services.memory_selfhosted import (
    ENV_MEMORY_HALF_LIFE_DAYS,
    ENV_MEMORY_TTL_DAYS,
    SelfHostedMemoryBackend,
    recall_env_for,
)
from tests.conftest import set_default_resources, ws_ctx
from tests.memory_fakes import FakeBedrockRuntime, FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "lambdas" / "memory_extractor"
T0 = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
BUCKET = "bkt"
STORE = "launchpad_memory-default"
ACTOR = "agent1__river"
FACTS = f"/facts/{ACTOR}"
PREFS = f"/preferences/{ACTOR}"
MEM = core.memory_index("")
SUBSTRATE = {
    "artifacts_bucket": BUCKET, "memory_id": STORE, "memory_bucket": BUCKET,
    "memory_prefix": "memory", "memory_os_endpoint": "search-mem.example",
    "memory_queue_url": "https://sqs.example/123/launchpad-memory-extract",
}


@pytest.fixture(scope="module")
def handler():
    sys.path.insert(0, str(LAMBDA_DIR))
    spec = importlib.util.spec_from_file_location("memory_extractor_v2", LAMBDA_DIR / "handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.path.remove(str(LAMBDA_DIR))


def _deps(handler, outputs, now=T0):
    emb, os_ = FakeEmbedder(), FakeOpenSearch()
    os_.ensure_index(MEM, core.memory_index_body())
    os_.ensure_index(core.jobs_index(""), core.jobs_index_body())
    return handler.Deps(
        s3=FakeS3(), opensearch=os_, bedrock=FakeBedrockRuntime(emb, outputs), embedder=emb,
        now=lambda: now, env={"extract_model": "global.zai.glm-5.3", "context_events": 4},
    )


def _event(deps, prompt, answer, session="sess-1", ts=T0):
    ev = core.turn_event(ACTOR, session, prompt, answer, ts)
    key = f"memory/{STORE}/{ACTOR}/session_{session}/events/{core.event_key(ts, ev['eventId'][:8])}"
    deps.s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(ev).encode())
    return key, ev


# The default `actor_summary_interval` is 1 (memory recall freshness, 2026-10-29): a test that
# scripts exactly three model outputs (facts, preferences, summary) pins the roll out with this.
NO_ROLL = {"actor_summary_interval": 5}


def _msg(key, merge=None, session="sess-1"):
    body = {"kind": "memory_extract", "store_id": STORE, "workspace_id": "default",
            "actor_id": ACTOR, "session_id": session, "event_key": key, "long_term": True,
            "bucket": BUCKET}
    if merge is not None:
        body["merge"] = merge
    return body


def _seed(target, namespace, text, rid, *, strategy="semantic", ts=T0, embedder=None, **extra):
    os_ = target.opensearch if hasattr(target, "opensearch") else target
    emb = embedder or (target.embedder if hasattr(target, "embedder") else FakeEmbedder())
    doc = {"store_id": STORE, "workspace_id": "default", "actor_id": ACTOR, "namespace": namespace,
           "strategy": strategy, "session_id": None, "text": text, "embedding": emb.embed(text),
           "created_at": ts.isoformat(), "source_event_ids": ["e0"], "metadata": {}, **extra}
    os_.put_doc(MEM, rid, doc)
    return doc


def _docs(deps, namespace=None):
    return {rid: d for rid, d in deps.opensearch.indices[MEM].items()
            if namespace is None or d["namespace"] == namespace}


def _ops(*items):
    return json.dumps(list(items))


def _user_message(deps, call_index):
    return deps.bedrock.converse_calls[call_index]["messages"][0]["content"][0]["text"]


# ── core helpers ──────────────────────────────────────────────────────────────────


def test_normalize_merge_settings_fills_defaults_clamps_and_drops_unknown_keys():
    assert core.normalize_merge_settings(None) == core.MERGE_SETTINGS_DEFAULTS
    out = core.normalize_merge_settings({
        "recall_half_life_days": 0, "max_facts_per_actor": "50", "long_term_ttl_days": 99999,
        "actor_summary_interval": None, "bogus": 1, "max_preferences_per_actor": "x",
    })
    assert out == {"recall_half_life_days": 1, "max_facts_per_actor": 50,
                   "max_preferences_per_actor": 100, "long_term_ttl_days": 3650,
                   "actor_summary_interval": 1}
    assert core.normalize_merge_settings({"long_term_ttl_days": None})["long_term_ttl_days"] is None


def test_rank_records_weights_recency_and_confidence_and_drops_inactive():
    emb = FakeEmbedder()
    vec = emb.embed("User likes tea")
    old = {"_id": "old", "text": "t", "embedding": vec,
           "created_at": (T0 - timedelta(days=180)).isoformat()}
    fresh = {"_id": "new", "text": "t", "embedding": vec,
             "created_at": (T0 - timedelta(days=200)).isoformat(), "updated_at": T0.isoformat()}
    shaky = {"_id": "shaky", "text": "t", "embedding": vec, "created_at": T0.isoformat(),
             "confidence": 0.2}
    gone = {"_id": "gone", "text": "t", "embedding": vec, "created_at": T0.isoformat(),
            "superseded_by": "new"}
    legacy = {"_id": "legacy", "text": "t", "embedding": vec}  # no stamps: decays to the floor
    ranked = core.rank_records([old, fresh, shaky, gone, legacy], vec, T0, 10, half_life_days=90)
    ids = [r["_id"] for r in ranked]
    assert "gone" not in ids and ids[0] == "new" and ids.index("old") < ids.index("legacy")
    by_id = {r["_id"]: r for r in ranked}
    assert by_id["new"]["recall_score"] == pytest.approx(1.0)
    assert by_id["old"]["recall_score"] == pytest.approx(0.25)  # two half-lives
    assert by_id["shaky"]["recall_score"] == pytest.approx(core.RECALL_CONFIDENCE_FLOOR)
    assert by_id["legacy"]["recall_score"] < 1e-6 and core.record_confidence(legacy) == 1.0
    assert all("embedding" not in r and r["similarity"] == pytest.approx(1.0) for r in ranked)
    assert [r["_id"] for r in core.rank_records([old, fresh], vec, T0, 1)] == ["new"]


def test_ttl_cutoff_filter_keeps_fresh_records_by_either_stamp():
    assert core.ttl_cutoff_filter(T0, None) is None
    os_ = FakeOpenSearch()
    os_.ensure_index(MEM, core.memory_index_body())
    _seed(os_, FACTS, "old", "old", ts=T0 - timedelta(days=40))
    _seed(os_, FACTS, "fresh", "fresh", ts=T0 - timedelta(days=1))
    _seed(os_, FACTS, "refreshed", "refreshed", ts=T0 - timedelta(days=40),
          updated_at=(T0 - timedelta(days=2)).isoformat())
    clause = core.ttl_cutoff_filter(T0, 30)
    hits = os_.search(MEM, {"query": {"bool": {"filter": [clause]}}})["hits"]["hits"]
    assert sorted(h["_id"] for h in hits) == ["fresh", "refreshed"]


# ── extractor: operation intent ──────────────────────────────────────────────────


class MapEmbedder(FakeEmbedder):
    """FakeEmbedder with pinned vectors for chosen texts, so a test controls cosines exactly
    (e.g. the 0.75–0.92 gray zone) instead of relying on token-bag overlap."""

    def __init__(self, pinned):
        self.pinned = pinned

    def embed(self, text):
        if text in self.pinned:
            return list(self.pinned[text])
        return super().embed(text)


def _axis(*weights):
    vec = [0.0] * FakeEmbedder.DIMS
    for i, w in enumerate(weights):
        vec[i] = w
    return vec


def _with_embedder(handler, outputs, pinned):
    deps = _deps(handler, outputs)
    emb = MapEmbedder(pinned)
    deps.embedder = emb
    deps.bedrock.embedder = emb
    return deps


def test_candidates_are_retrieved_before_the_model_call_and_inactive_ones_never_shown(handler):
    deps = _deps(handler, ["[]", "[]", "s"])
    _seed(deps, FACTS, "User lives in Shanghai", "f1")
    _seed(deps, FACTS, "User owns two cats", "f2", superseded_by="x")
    _seed(deps, PREFS, json.dumps({"preference": "short answers", "category": "style"}), "p1",
          strategy="user_preference")
    key, _ = _event(deps, "Where do I live again?", "Shanghai.")
    out = handler.process_message(_msg(key), deps)
    facts_msg, prefs_msg = _user_message(deps, 0), _user_message(deps, 1)
    assert facts_msg.startswith("EXISTING:\n") and "CONVERSATION:\n" in facts_msg
    existing = json.loads(facts_msg.split("\n")[1])
    assert existing == [{"id": "c1", "text": "User lives in Shanghai"}]  # f2 is superseded
    assert json.loads(prefs_msg.split("\n")[1]) == [
        {"id": "c1", "preference": "short answers", "category": "style"}
    ]
    assert out["counts"]["facts"] == 0 and out["counts"]["noops"] == 0


def test_update_rewrites_the_target_in_place_keeping_its_id(handler):
    pinned = {"User lives in Shanghai": _axis(1.0), "User lives in Hangzhou": _axis(0.0, 1.0)}
    deps = _with_embedder(handler, [_ops({"op": "update", "target_id": "c1", "confidence": 0.9,
                                          "text": "User lives in Hangzhou"}), "[]", "s"], pinned)
    _seed(deps, FACTS, "User lives in Shanghai", "f1", ts=T0 - timedelta(days=10))
    key, ev = _event(deps, "I moved to Hangzhou.", "Noted.")
    out = handler.process_message(_msg(key), deps)
    assert out["counts"]["updates"] == 1 and out["counts"]["facts"] == 0
    (doc,) = _docs(deps, FACTS).values()
    assert doc is deps.opensearch.indices[MEM]["f1"]
    assert doc["text"] == "User lives in Hangzhou" and doc["embedding"] == _axis(0.0, 1.0)
    assert doc["created_at"] == (T0 - timedelta(days=10)).isoformat()
    assert doc["updated_at"] == T0.isoformat() and doc["last_op"] == "update"
    assert doc["confidence"] == 0.9 and doc["source_event_ids"] == ["e0", ev["eventId"]]


def test_supersede_writes_a_new_record_and_marks_the_target(handler):
    pinned = {"User works at Acme": _axis(1.0), "User works at Globex": _axis(0.0, 1.0)}
    deps = _with_embedder(handler, [_ops({"op": "supersede", "target_id": "c1",
                                          "confidence": 0.95, "text": "User works at Globex"}),
                                    "[]", "s"], pinned)
    _seed(deps, FACTS, "User works at Acme", "f1")
    key, _ = _event(deps, "I left Acme, I'm at Globex now.", "Congrats!")
    out = handler.process_message(_msg(key), deps)
    assert out["counts"]["supersedes"] == 1
    docs = _docs(deps, FACTS)
    (new_id,) = [rid for rid in docs if rid != "f1"]
    assert docs[new_id]["text"] == "User works at Globex" and docs[new_id]["supersedes"] == ["f1"]
    assert docs[new_id]["last_op"] == "supersede" and core.record_is_active(docs[new_id])
    assert docs["f1"]["superseded_by"] == new_id and docs["f1"]["superseded_at"] == T0.isoformat()
    assert not core.record_is_active(docs["f1"]) and docs["f1"]["text"] == "User works at Acme"


def test_delete_is_soft_and_needs_a_known_target_and_confidence(handler):
    deps = _deps(handler, [_ops({"op": "delete", "target_id": "c1", "confidence": 0.9},
                                {"op": "delete", "target_id": "c9", "confidence": 0.9}),
                           _ops({"op": "delete", "target_id": "c1", "confidence": 0.3}), "s"])
    _seed(deps, FACTS, "User has a dog", "f1")
    _seed(deps, PREFS, json.dumps({"preference": "emoji", "category": "style"}), "p1",
          strategy="user_preference")
    key, _ = _event(deps, "Forget that I have a dog.", "Done.")
    out = handler.process_message(_msg(key), deps)
    assert out["counts"]["deletes"] == 1 and out["counts"]["noops"] == 2
    f1 = deps.opensearch.indices[MEM]["f1"]  # never hard-deleted by the model
    assert f1["deleted_at"] == T0.isoformat() and f1["last_op"] == "delete"
    assert core.record_is_active(deps.opensearch.indices[MEM]["p1"])
    job = deps.opensearch.get_doc(core.jobs_index(""), out["job_id"])
    assert sum("delete without a known target" in w for w in job["warnings"]) == 2


def test_delete_without_a_stated_confidence_is_ignored_but_other_ops_keep_the_default(handler):
    """rev-r11 nit on r14-a: an absent `confidence` reads as 0 for `delete` (below the 0.6
    floor → noop + warning), while an `add` without one still lands at 0.8."""
    deps = _deps(handler, [_ops({"op": "delete", "target_id": "c1"},
                                {"op": "add", "text": "User plays go"}), "[]", "s"])
    _seed(deps, FACTS, "User has a dog", "f1")
    key, _ = _event(deps, "Forget the dog.", "Done.")
    out = handler.process_message(_msg(key), deps)
    assert out["counts"]["deletes"] == 0 and out["counts"]["noops"] == 1
    assert out["counts"]["facts"] == 1
    docs = _docs(deps, FACTS)
    assert core.record_is_active(docs["f1"]) and "deleted_at" not in docs["f1"]
    (added,) = [d for d in docs.values() if d["text"] == "User plays go"]
    assert added["confidence"] == handler.DEFAULT_CONFIDENCE
    job = deps.opensearch.get_doc(core.jobs_index(""), out["job_id"])
    assert any("delete without a known target or confidence" in w for w in job["warnings"])


def test_update_or_supersede_without_a_known_target_degrades_to_add_and_noop_is_counted(handler):
    deps = _deps(handler, [_ops({"op": "update", "target_id": "c7", "text": "User plays chess"},
                                {"op": "supersede", "target_id": None, "text": "User reads sci-fi"},
                                {"op": "noop"}, {"op": "bogus", "text": "User swims"}),
                           "[]", "s"])
    key, _ = _event(deps, "chess, sci-fi, swimming", "nice")
    out = handler.process_message(_msg(key), deps)
    assert out["counts"]["facts"] == 3 and out["counts"]["noops"] == 1
    assert out["counts"]["updates"] == out["counts"]["supersedes"] == 0
    assert {d["last_op"] for d in _docs(deps, FACTS).values()} == {"add"}


def test_dedupe_at_0_92_wins_over_any_model_op(handler):
    pinned = {"User likes tea": _axis(1.0), "User really likes tea": _axis(0.95, 0.312)}
    deps = _with_embedder(handler, [_ops({"op": "supersede", "target_id": "c1",
                                          "text": "User really likes tea"}), "[]", "s"], pinned)
    _seed(deps, FACTS, "User likes tea", "f1")
    key, _ = _event(deps, "tea!", "ok")
    out = handler.process_message(_msg(key), deps)
    assert out["counts"]["duplicates"] == 1 and out["counts"]["supersedes"] == 0
    assert list(_docs(deps, FACTS)) == ["f1"] and core.record_is_active(_docs(deps, FACTS)["f1"])


def test_gray_zone_add_is_honoured_and_both_records_stay_active(handler):
    pinned = {"User likes green tea": _axis(1.0), "User likes oolong": _axis(0.8, 0.6)}
    assert core.MERGE_GRAY_ZONE_LOW <= core.cosine(_axis(1.0), _axis(0.8, 0.6)) < core.DEDUPE_COSINE
    deps = _with_embedder(handler, [_ops({"op": "add", "text": "User likes oolong"}), "[]", "s"],
                          pinned)
    _seed(deps, FACTS, "User likes green tea", "f1")
    key, _ = _event(deps, "Oolong too.", "ok")
    out = handler.process_message(_msg(key), deps)
    assert out["counts"]["facts"] == 1
    assert len(_docs(deps, FACTS)) == 2 and all(core.record_is_active(d)
                                                for d in _docs(deps, FACTS).values())


# ── extractor: caps, TTL, rolling summary ─────────────────────────────────────────


def _seed_many(deps, n):
    ids = []
    for i in range(n):
        rid = f"f{i:02d}"
        _seed(deps, FACTS, f"fact number {i} about topic{i}", rid, ts=T0 - timedelta(days=n - i))
        ids.append(rid)
    return ids  # oldest first


def test_consolidation_merges_the_oldest_records_when_the_cap_is_exceeded(handler):
    groups = _ops({"text": "User likes tea and coffee", "merges": ["r1", "r2"], "confidence": 0.9},
                  {"text": "lonely", "merges": ["r3"]},           # one member → ignored
                  {"text": "ghost", "merges": ["r98", "r99"]},    # unknown ids → ignored
                  {"text": "again", "merges": ["r1", "r4"]})      # r1 already used → ignored
    deps = _deps(handler, ["[]", "[]", "s", groups])
    ids = _seed_many(deps, 12)
    key, _ = _event(deps, "hi", "hello")
    out = handler.process_message(_msg(key, merge={"max_facts_per_actor": 10, **NO_ROLL}), deps)
    assert out["counts"]["consolidated"] == 2
    listing = json.loads(_user_message(deps, 3))
    assert [r["id"] for r in listing] == ["r1", "r2", "r3", "r4"]  # 2 × overflow, ≥ 4
    docs = _docs(deps, FACTS)
    (merged_id,) = [rid for rid, d in docs.items() if d.get("last_op") == "consolidate"]
    assert docs[merged_id]["supersedes"] == ids[:2] and docs[merged_id]["confidence"] == 0.9
    assert all(docs[rid]["superseded_by"] == merged_id for rid in ids[:2])
    assert sum(core.record_is_active(d) for d in docs.values()) == 11
    assert len(deps.bedrock.converse_calls) == 4  # preferences were under their cap


def test_consolidation_is_bounded_to_twenty_records_per_event(handler):
    deps = _deps(handler, ["[]", "[]", "s", "[]"])
    _seed_many(deps, 40)
    key, _ = _event(deps, "hi", "hello")
    handler.process_message(_msg(key, merge={"max_facts_per_actor": 10}), deps)
    assert len(json.loads(_user_message(deps, 3))) == core.CONSOLIDATE_MAX_RECORDS


def test_no_consolidation_under_the_cap_and_garbage_output_only_warns(handler):
    deps = _deps(handler, ["[]", "[]", "s", "not json"])
    _seed_many(deps, 11)
    key, _ = _event(deps, "hi", "hello")
    out = handler.process_message(_msg(key, merge=NO_ROLL), deps)  # default cap 200
    assert len(deps.bedrock.converse_calls) == 3 and out["counts"]["consolidated"] == 0
    key2, _ = _event(deps, "hi", "hello", ts=T0 + timedelta(minutes=1))
    deps.bedrock.outputs = ["[]", "[]", "s", "not json"]
    out2 = handler.process_message(_msg(key2, merge={"max_facts_per_actor": 10, **NO_ROLL}), deps)
    assert out2["status"] == "COMPLETED" and out2["counts"]["consolidated"] == 0
    job = deps.opensearch.get_doc(core.jobs_index(""), out2["job_id"])
    assert any("consolidation output unparseable" in w for w in job["warnings"])


def test_ttl_soft_expires_old_records_but_never_the_actor_summary(handler):
    deps = _deps(handler, ["[]", "[]", "s"])
    _seed(deps, FACTS, "ancient fact", "old", ts=T0 - timedelta(days=40))
    _seed(deps, FACTS, "recent fact", "new", ts=T0 - timedelta(days=2))
    _seed(deps, PREFS, json.dumps({"preference": "old pref"}), "oldp", strategy="user_preference",
          ts=T0 - timedelta(days=100), updated_at=(T0 - timedelta(days=5)).isoformat())
    actor_ns = core.actor_summary_namespace(ACTOR)
    _seed(deps, actor_ns, json.dumps({"summary": "rolled"}), "actor", strategy="summarization",
          ts=T0 - timedelta(days=90))
    key, _ = _event(deps, "hi", "hello")
    out = handler.process_message(_msg(key, merge={"long_term_ttl_days": 30}), deps)
    docs = deps.opensearch.indices[MEM]
    assert out["counts"]["expired"] == 1 and docs["old"]["expired_at"] == T0.isoformat()
    assert "old" in docs  # soft — never removed
    assert all("expired_at" not in docs[r] for r in ("new", "oldp", "actor"))


def test_no_ttl_means_nothing_expires(handler):
    deps = _deps(handler, ["[]", "[]", "s"])
    _seed(deps, FACTS, "ancient fact", "old", ts=T0 - timedelta(days=4000))
    key, _ = _event(deps, "hi", "hello")
    assert handler.process_message(_msg(key), deps)["counts"]["expired"] == 0


def _seed_completed_jobs(deps, n):
    for i in range(n):
        deps.opensearch.put_doc(core.jobs_index(""), f"j{i}", {
            "job_id": f"j{i}", "kind": "memory_extract", "store_id": STORE, "actor_id": ACTOR,
            "status": "COMPLETED", "created_at": T0.isoformat()})


def test_rolling_actor_summary_every_n_extractions_from_recent_session_summaries(handler):
    long_text = " ".join(f"w{i}" for i in range(200))
    deps = _deps(handler, ["[]", "[]", "Talked about tea.", long_text])
    _seed(deps, f"/summaries/{ACTOR}/s-old", json.dumps({"summary": "Planned a trip."}), "s1",
          strategy="summarization", ts=T0 - timedelta(days=1))
    _seed_completed_jobs(deps, 4)  # this extraction is the 5th → interval 5 rolls
    key, _ = _event(deps, "tea", "ok")
    out = handler.process_message(_msg(key, merge=NO_ROLL), deps)
    assert out["counts"]["actor_summary"] == 1
    sources = _user_message(deps, 3)
    assert "- Talked about tea." in sources and "- Planned a trip." in sources
    rid = core.summary_record_id(STORE, core.actor_summary_namespace(ACTOR))
    doc = deps.opensearch.indices[MEM][rid]
    words = json.loads(doc["text"])["summary"].split()
    assert len(words) == core.ACTOR_SUMMARY_MAX_WORDS and words[0] == "w0"
    assert doc["namespace"] == core.actor_summary_namespace(ACTOR)
    assert doc["metadata"] == {"kind": "actor_summary", "sources": 2}


def test_actor_summary_is_skipped_between_intervals(handler):
    deps = _deps(handler, ["[]", "[]", "s"])
    _seed_completed_jobs(deps, 2)
    key, _ = _event(deps, "tea", "ok")
    out = handler.process_message(_msg(key, merge=NO_ROLL), deps)
    assert out["counts"]["actor_summary"] == 0 and len(deps.bedrock.converse_calls) == 3
    # the default interval is 1 (memory recall freshness, 2026-10-29): the very first
    # extraction of an actor already rolls its summary — no merge block needed
    deps2 = _deps(handler, ["[]", "[]", "s", "every time"])
    key2, _ = _event(deps2, "tea", "ok")
    out2 = handler.process_message(_msg(key2), deps2)
    assert out2["counts"]["actor_summary"] == 1 and len(deps2.bedrock.converse_calls) == 4


# ── hub: recall ranking, listing, rail, settings ─────────────────────────────────


@pytest.fixture
def fakes():
    return {"s3": FakeS3(), "opensearch": FakeOpenSearch(), "sqs": FakeSQS(),
            "embedder": FakeEmbedder()}


@pytest.fixture
def backend(client, fakes):  # `client` → init_db ran (memory_stores exists)
    fakes["opensearch"].ensure_index(MEM, core.memory_index_body())
    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    return SelfHostedMemoryBackend(**fakes, clock=lambda: T0)


@pytest.fixture
def ws():
    return ws_ctx(SUBSTRATE, region="cn-northwest-1")


def test_search_ranks_by_recency_and_hides_inactive_records(backend, ws, fakes):
    os_, emb = fakes["opensearch"], fakes["embedder"]
    _seed(os_, FACTS, "User likes green tea", "stale", embedder=emb, ts=T0 - timedelta(days=400))
    _seed(os_, FACTS, "User likes green tea", "fresh", embedder=emb, ts=T0 - timedelta(days=1))
    _seed(os_, FACTS, "User likes green tea", "replaced", embedder=emb, superseded_by="fresh")
    _seed(os_, FACTS, "User likes green tea", "retracted", embedder=emb,
          deleted_at=T0.isoformat())
    out = backend.search_records(ws, FACTS, "User likes green tea", 5)
    assert [r["record_id"] for r in out["items"]] == ["fresh", "stale"]
    top = out["items"][0]
    assert top["similarity"] == pytest.approx(1.0) and top["score"] < top["similarity"]
    assert top["active"] is True and top["confidence"] == 1.0
    assert "embedding" not in json.dumps(out)
    hits = backend.retrieve_records(ws, FACTS, "green tea", 1)
    assert [r["record_id"] for r in hits] == ["fresh"]


def test_listing_hides_superseded_by_default_and_shows_them_on_request(backend, ws, fakes):
    os_ = fakes["opensearch"]
    _seed(os_, FACTS, "User works at Globex", "new", supersedes=["old"], last_op="supersede",
          updated_at=T0.isoformat(), confidence=0.9)
    _seed(os_, FACTS, "User works at Acme", "old", superseded_by="new",
          superseded_at=T0.isoformat())
    _seed(os_, FACTS, "User had a dog", "gone", deleted_at=T0.isoformat())
    _seed(os_, FACTS, "Ancient", "exp", expired_at=T0.isoformat())
    assert [r["record_id"] for r in backend.list_records(ws, FACTS)["items"]] == ["new"]
    rows = {r["record_id"]: r for r in
            backend.list_records(ws, FACTS, include_superseded=True)["items"]}
    assert set(rows) == {"new", "old", "gone", "exp"}
    assert rows["old"]["superseded_by"] == "new" and rows["old"]["active"] is False
    assert rows["new"]["supersedes"] == ["old"] and rows["new"]["last_op"] == "supersede"
    assert rows["new"]["confidence"] == 0.9 and rows["new"]["updated_at"] == T0.isoformat()
    assert rows["gone"]["deleted_at"] and rows["exp"]["expired_at"]


def test_store_ttl_hides_old_records_on_read_without_writing(backend, ws, fakes):
    os_ = fakes["opensearch"]
    _seed(os_, FACTS, "old fact", "old", ts=T0 - timedelta(days=40))
    _seed(os_, FACTS, "new fact", "new", ts=T0 - timedelta(days=1))
    assert len(backend.list_records(ws, FACTS)["items"]) == 2
    backend.update_memory_resource(ws, STORE, merge={"long_term_ttl_days": 30})
    assert [r["record_id"] for r in backend.list_records(ws, FACTS)["items"]] == ["new"]
    found = backend.search_records(ws, FACTS, "fact", 5)["items"]
    assert [r["record_id"] for r in found] == ["new"]
    assert "expired_at" not in os_.indices[MEM]["old"]  # read-side only


def test_chat_rail_hides_superseded_records(backend, ws, fakes):
    os_ = fakes["opensearch"]
    _seed(os_, FACTS, "User works at Globex", "new")
    _seed(os_, FACTS, "User works at Acme", "old", superseded_by="new")
    out = backend.session_memory_summary(ws, ACTOR, "sess-1")
    assert [r["text"] for r in out["records"]] == ["User works at Globex"]


def test_merge_settings_persist_patch_reset_and_ride_the_extract_message(backend, ws, fakes):
    assert backend.get_memory_resource(ws, STORE)["merge"] == core.MERGE_SETTINGS_DEFAULTS
    created = backend.create_memory_resource(ws, "team", "", 30, ["semantic"],
                                             merge={"max_facts_per_actor": 50})
    assert created["merge"] == {**core.MERGE_SETTINGS_DEFAULTS, "max_facts_per_actor": 50}
    out = backend.update_memory_resource(ws, created["id"], merge={
        "long_term_ttl_days": 60, "recall_half_life_days": 30})
    assert out["merge"]["long_term_ttl_days"] == 60 and out["merge"]["max_facts_per_actor"] == 50
    out = backend.update_memory_resource(ws, created["id"], merge={"max_facts_per_actor": None})
    assert out["merge"]["max_facts_per_actor"] == 200  # null resets to the default
    backend.update_memory_resource(ws, STORE, merge={"actor_summary_interval": 2})
    backend.create_turn_event(ws, ACTOR, "sess-1", "hi", "hello")
    ((_, msg),) = fakes["sqs"].sent
    assert msg["merge"]["actor_summary_interval"] == 2


def test_recall_env_is_written_only_for_non_default_knobs(backend, ws):
    assert recall_env_for(STORE) == {} and recall_env_for("nope") == {}
    backend.update_memory_resource(ws, STORE, merge={"recall_half_life_days": 30,
                                                     "long_term_ttl_days": 365})
    assert recall_env_for(STORE) == {ENV_MEMORY_HALF_LIFE_DAYS: "30", ENV_MEMORY_TTL_DAYS: "365"}
    from app.deployer.environment import runtime_environment
    from app.schemas.agent import AgentSpec, MemoryConfig

    spec = AgentSpec(name="mem", method="zip_runtime", system_prompt="x",
                     memory=MemoryConfig(short_term=True, long_term=True))
    env = runtime_environment(spec, SUBSTRATE)
    assert env[ENV_MEMORY_HALF_LIFE_DAYS] == "30" and env[ENV_MEMORY_TTL_DAYS] == "365"
    short = AgentSpec(name="mem2", method="zip_runtime", system_prompt="x",
                      memory=MemoryConfig(short_term=True, long_term=False))
    assert ENV_MEMORY_HALF_LIFE_DAYS not in runtime_environment(short, SUBSTRATE)


def test_api_validates_and_applies_merge_settings(client, fakes):
    fakes["opensearch"].ensure_index(MEM, core.memory_index_body())
    set_default_resources(SUBSTRATE)
    memory_backend.set_backend(SelfHostedMemoryBackend(**fakes, clock=lambda: T0))
    url = f"/api/memory/resources/{STORE}"
    ok = client.put(url, json={"merge": {"recall_half_life_days": 45, "long_term_ttl_days": None}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["merge"]["recall_half_life_days"] == 45
    assert ok.json()["merge"]["long_term_ttl_days"] is None
    for bad in ({"merge": {"max_facts_per_actor": 5}}, {"merge": {"long_term_ttl_days": 1}},
                {"merge": {"bogus": 1}}, {"merge": {}}):
        res = client.put(url, json=bad)
        assert res.status_code == 422, (bad, res.text)
    created = client.post("/api/memory/resources", json={
        "name": "capped", "strategies": ["semantic"], "merge": {"max_preferences_per_actor": 20}})
    assert created.status_code == 201, created.text
    assert created.json()["merge"]["max_preferences_per_actor"] == 20
    _seed(fakes["opensearch"], FACTS, "User works at Acme", "old", superseded_by="new")
    _seed(fakes["opensearch"], FACTS, "User works at Globex", "new")
    params = {"actor_id": ACTOR, "strategy_id": "semantic"}
    listed = client.get("/api/memory/records", params=params).json()["items"]
    assert [r["record_id"] for r in listed] == ["new"]
    full = client.get("/api/memory/records", params={**params, "include_superseded": "true"})
    assert {r["record_id"] for r in full.json()["items"]} == {"new", "old"}


# ── mapping additivity on a legacy index ──────────────────────────────────────────


def _legacy_body():
    body = core.memory_index_body()
    props = body["mappings"]["properties"]
    for field in core.MEMORY_RECORD_V2_PROPERTIES:
        props.pop(field)
    return body


def test_ensure_mapping_adds_the_v2_fields_once_and_legacy_docs_read_as_active(backend, ws, fakes):
    os_ = FakeOpenSearch()
    os_.ensure_index(MEM, _legacy_body())
    legacy = {"store_id": STORE, "actor_id": ACTOR, "namespace": FACTS, "strategy": "semantic",
              "text": "User likes tea", "embedding": FakeEmbedder().embed("User likes tea"),
              "created_at": T0.isoformat()}
    os_.put_doc(MEM, "legacy", legacy)
    added = opensearch_ensure.ensure_mapping(os_, MEM, core.MEMORY_RECORD_V2_PROPERTIES)
    assert added == sorted(core.MEMORY_RECORD_V2_PROPERTIES)
    live = os_.bodies[MEM]["mappings"]["properties"]
    assert all(live[f] == spec for f, spec in core.MEMORY_RECORD_V2_PROPERTIES.items())
    assert opensearch_ensure.ensure_mapping(os_, MEM, core.MEMORY_RECORD_V2_PROPERTIES) == []
    puts = [r for r in os_.requests if r["method"] == "PUT"]
    assert len(puts) == 1 and os_.indices[MEM]["legacy"] == legacy  # no reindex, no doc write
    hub = SelfHostedMemoryBackend(s3=FakeS3(), opensearch=os_, sqs=FakeSQS(),
                                  embedder=FakeEmbedder(), clock=lambda: T0)
    (row,) = hub.list_records(ws, FACTS)["items"]
    assert row["active"] is True and row["confidence"] == 1.0 and row["updated_at"] is None
    assert hub.search_records(ws, FACTS, "User likes tea", 3)["items"][0]["record_id"] == "legacy"


def test_ensure_mapping_on_a_missing_index_raises():
    with pytest.raises(core.OpenSearchError):
        opensearch_ensure.ensure_mapping(FakeOpenSearch(), MEM, core.MEMORY_RECORD_V2_PROPERTIES)


# ── runtime library: recall ranking + actor summary line ─────────────────────────


@pytest.fixture
def mod(tmp_path, monkeypatch):
    from tests.test_memory_runtime import MEM_ENV, SPEC, _load

    for k, v in MEM_ENV.items():
        monkeypatch.setenv(k, v)
    return _load(SPEC, tmp_path, monkeypatch, "mem_main_v2")


def _rt_seed(mod, rid, ns, text, **extra):
    emb = mod._fakes["emb"]
    mod._fakes["os"].put_doc(MEM, rid, {"store_id": STORE, "namespace": ns, "text": text,
                                        "embedding": emb.embed(text), **extra})


def test_runtime_recall_excludes_superseded_and_appends_the_actor_summary(mod):
    now = datetime.now(UTC)
    _rt_seed(mod, "new", FACTS, "User works at Globex", created_at=now.isoformat())
    _rt_seed(mod, "old", FACTS, "User works at Acme", created_at=now.isoformat(),
             superseded_by="new")
    _rt_seed(mod, "gone", PREFS, "Likes emoji", created_at=now.isoformat(),
             deleted_at=now.isoformat())
    out = mod.recall_context(ACTOR, "where does the user work")
    assert "Globex" in out and "Acme" not in out and "emoji" not in out
    assert "Summary of earlier conversations" not in out
    actor_ns = core.actor_summary_namespace(ACTOR)
    mod._fakes["os"].put_doc(MEM, core.summary_record_id(STORE, actor_ns), {
        "store_id": STORE, "namespace": actor_ns, "text": json.dumps({"summary": "Moved jobs."})})
    out = mod.recall_context(ACTOR, "where does the user work")
    assert out.splitlines()[-1] == "Summary of earlier conversations: Moved jobs."


def test_runtime_recall_prefers_recent_records_and_honours_env_knobs(mod, monkeypatch):
    now = datetime.now(UTC)
    stale, fresh = (now - timedelta(days=500)).isoformat(), (now - timedelta(days=1)).isoformat()
    _rt_seed(mod, "stale", FACTS, "User likes tea", created_at=stale)
    _rt_seed(mod, "fresh", FACTS, "User likes tea", created_at=fresh)
    rows = mod.make_memory_store(ACTOR).search("User likes tea", namespaces=[FACTS], top_k=1)
    assert len(rows) == 1 and rows[0]["created_at"] == fresh
    monkeypatch.setenv("LAUNCHPAD_MEMORY_TTL_DAYS", "30")
    rows = mod.make_memory_store(ACTOR).search("User likes tea", namespaces=[FACTS], top_k=5)
    assert len(rows) == 1  # the 500-day-old record is past the store TTL
    monkeypatch.setenv("LAUNCHPAD_MEMORY_HALF_LIFE_DAYS", "not-a-number")
    assert mod._memory_env()["half_life_days"] == core.RECALL_HALF_LIFE_DAYS
