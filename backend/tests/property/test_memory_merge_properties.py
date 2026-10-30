"""E06 — long-term memory merge strategy (v2): property, metamorphic and fault tests.

Round 16, channel A (BUG-DISCOVERY-PLAN §E06 / §4). Random fact sequences — operations
add / update / supersede / delete / noop with similarities sampled in 0.6–1.0, confidences
in 0–1 (or absent / null) and legacy documents without the v2 fields — are pushed through
the extractor Lambda's merge (`backend/lambdas/memory_extractor/handler.py`) against the
hermetic substrate (`tests/memory_fakes.py`). Every invariant cites its clause in
`docs/cn/interfaces/memory-backend.md` "Merge strategy (v2)" (or `memory-runtime.md`
"Recall"); no threshold is read off the implementation.

Oracles: a sequential model of the contract's decision table (differential), the
document set before / after one event (ledger delta == job counts), the index scan in
`tests/property/ledger_invariants.py`, the recall formula recomputed from the contract,
and a paraphrase metamorphic relation (same embedding → same op class). Failing shrunk
examples are findings and stay `xfail(strict=True)` under their N16 id.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import math
import sys
from datetime import UTC, datetime, timedelta
from itertools import count
from pathlib import Path
from typing import Any

import pytest
import urllib3
from hypothesis import HealthCheck, Phase, given, settings
from hypothesis import strategies as st

from app.core.db import DEFAULT_WORKSPACE_ID, init_db
from app.services import memory_backend
from app.services import opensearch_http as core
from app.services.memory_selfhosted import SelfHostedMemoryBackend
from tests.conftest import set_default_resources, ws_ctx
from tests.memory_fakes import FakeBedrockRuntime, FakeOpenSearch, FakeS3, FakeSQS, cosine
from tests.property.ledger_invariants import (
    active_ids,
    assert_memory_index_invariants,
    first_violation,
    record_is_active,
    scan_memory_index,
)

LAMBDA_DIR = Path(__file__).resolve().parents[2] / "lambdas" / "memory_extractor"
T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
BUCKET = "bkt"
STORE = "launchpad_memory-default"
ACTOR = "agent1__river"
FACTS = f"/facts/{ACTOR}"
PREFS = f"/preferences/{ACTOR}"
MEM = core.memory_index("")
JOBS = core.jobs_index("")
SUBSTRATE = {
    "artifacts_bucket": BUCKET,
    "memory_id": STORE,
    "memory_bucket": BUCKET,
    "memory_prefix": "memory",
    "memory_os_endpoint": "search-mem.example",
    "memory_queue_url": "https://sqs.example/123/launchpad-memory-extract",
}

# ── contract constants (memory-backend.md "Merge strategy (v2)") ───────────────────────
DEDUPE_COSINE = 0.92  # "nearest active record at cosine ≥ 0.92 → duplicate"
DELETE_MIN_CONFIDENCE = 0.6  # "`op = delete` with a candidate target and confidence ≥ 0.6"
CANDIDATE_K = 8  # "retrieves the actor's 8 nearest ACTIVE records"
CONSOLIDATE_MAX = 20  # "takes the oldest min(20, max(4, 2 × overflow)) records"
EXPIRE_MAX_PER_EVENT = 50  # "marks up to 50 such records of the actor expired_at per event"
RECALL_FLOOR = 0.5  # "max(confidence, 0.5)"
RECALL_HALF_LIFE_DEFAULT = 90  # per-store settings table: recall_half_life_days default 90
RECALL_OVERFETCH = (4, 20)  # "over-fetch min(4 × top_k, 20) k-NN hits"
CAP_MIN = 10  # per-store settings table: max_facts_per_actor bounds 10–10000
TTL_BOUNDS = (7, 3650)  # per-store settings table: long_term_ttl_days bounds 7–3650
DIMS = 16

_STORE_SEQ = count()
_HEALTH = [HealthCheck.function_scoped_fixture, HealthCheck.too_slow, HealthCheck.data_too_large]
# the sequence / cap / TTL properties run a whole extractor event per step — 60 examples keep
# the file under the gate's budget; the cheap single-event properties take 150
HYP = settings(max_examples=60, deadline=None, suppress_health_check=_HEALTH)
HYP_LIGHT = settings(max_examples=150, deadline=None, suppress_health_check=_HEALTH)
# a property that is EXPECTED to fail (a registered finding): generate only, never shrink —
# shrinking a known failure is what made the first run take minutes
HYP_XFAIL = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=_HEALTH,
    phases=[Phase.explicit, Phase.reuse, Phase.generate],
    # deterministic generation: a registered finding must reproduce on every run, under any
    # load, so the strict xfail below can never XPASS by chance (r16-k chain-gate lesson)
    derandomize=True,
)


@pytest.fixture(scope="module")
def handler():
    sys.path.insert(0, str(LAMBDA_DIR))
    spec = importlib.util.spec_from_file_location(
        "memory_extractor_prop", LAMBDA_DIR / "handler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.path.remove(str(LAMBDA_DIR))


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()  # the hub backend reads `memory_stores` for the per-store merge settings


# ── vectors ───────────────────────────────────────────────────────────────────────────


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v] if n else [1.0] + [0.0] * (len(v) - 1)


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))


def rand_unit(seed: int | str) -> list[float]:
    digest = hashlib.sha256(str(seed).encode()).digest()[:DIMS]
    return _norm([float(b - 128) for b in digest])


def with_cosine(u: list[float], s: float, seed: int) -> list[float]:
    """A unit vector at cosine exactly `s` (clamped to [-1, 1]) from unit vector `u`."""
    s = max(-1.0, min(1.0, s))
    w = rand_unit(("orth", seed))
    proj = _cos(w, u)
    w = _norm([wi - proj * ui for wi, ui in zip(w, u, strict=False)])
    k = math.sqrt(max(0.0, 1.0 - s * s))
    return _norm([s * ui + k * wi for ui, wi in zip(u, w, strict=False)])


def far_unit(existing: list[list[float]], seed: int, ceiling: float = 0.6) -> list[float]:
    """A unit vector below `ceiling` cosine from every vector in `existing`."""
    for i in range(200):
        v = rand_unit((seed, i))
        if all(abs(_cos(v, e)) < ceiling for e in existing):
            return v
    raise AssertionError("could not place a fresh vector")  # pragma: no cover


class VectorEmbedder:
    """`embed(text)` returns the vector the test assigned to `text` (else a deterministic
    pseudo-random unit vector) — the test decides every similarity."""

    DIMS = DIMS
    model_id = "test-vectors"

    def __init__(self) -> None:
        self.table: dict[str, list[float]] = {}

    def assign(self, text: str, vector: list[float]) -> None:
        self.table[text] = list(vector)

    def embed(self, text: str) -> list[float]:
        return list(self.table.get(text) or rand_unit(("text", text)))


# ── the world: extractor deps + hub backend over one shared fake substrate ─────────────


class World:
    def __init__(self, handler: Any, *, store: str = STORE, now: datetime = T0) -> None:
        self.h = handler
        self.store = store
        self.now = now
        self.emb = VectorEmbedder()
        self.os = FakeOpenSearch()
        self.os.ensure_index(MEM, core.memory_index_body())
        self.os.ensure_index(JOBS, core.jobs_index_body())
        self.s3 = FakeS3()
        self.bedrock = FakeBedrockRuntime(self.emb, [])
        self.deps = handler.Deps(
            s3=self.s3,
            opensearch=self.os,
            bedrock=self.bedrock,
            embedder=self.emb,
            now=lambda: self.now,
            env={"extract_model": "global.zai.glm-5.3", "context_events": 0},
        )
        self.events = 0
        self.emb.assign("conversation", rand_unit("transcript"))

    # documents ------------------------------------------------------------------
    @property
    def docs(self) -> dict[str, dict[str, Any]]:
        return {rid: dict(d) for rid, d in self.os.indices[MEM].items()}

    def seed(
        self,
        rid: str | None,
        text: str,
        vector: list[float],
        *,
        namespace: str = FACTS,
        strategy: str = "semantic",
        legacy: bool = False,
        created: datetime | None = None,
        updated: datetime | None = None,
        confidence: float | None = 0.8,
        **extra: Any,
    ) -> dict[str, Any]:
        self.emb.assign(text, vector)
        created = created or self.now
        # production ids derive from (store, namespace, normalized text) — seed the same way
        # so an id collision the extractor would hit live is reproduced here
        rid = rid or self.h._record_id(self.store, namespace, text)
        doc: dict[str, Any] = {
            "store_id": self.store,
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "actor_id": ACTOR,
            "namespace": namespace,
            "strategy": strategy,
            "session_id": None,
            "text": text,
            "embedding": list(vector),
            "created_at": created.isoformat(),
            "source_event_ids": ["e0"],
            "metadata": {},
        }
        if not legacy:  # a pre-v2 document carries none of these (contract: reads as active, 1.0)
            doc.update(
                {
                    "updated_at": (updated or created).isoformat(),
                    "confidence": confidence,
                    "last_op": "add",
                }
            )
        elif updated is not None:
            doc["updated_at"] = updated.isoformat()
        doc.update(extra)
        self.os.put_doc(MEM, rid, doc)
        return {"_id": rid, **doc}

    def candidates(self, namespace: str = FACTS) -> list[dict[str, Any]]:
        """What the extractor shows the model: the `CANDIDATE_K` nearest ACTIVE records of
        the namespace by similarity to the transcript (contract: "8 nearest ACTIVE")."""
        hits = self.os.knn_search(
            MEM,
            self.emb.embed("conversation"),
            CANDIDATE_K,
            [
                {"term": {"store_id": self.store}},
                {"term": {"namespace": namespace}},
                core.active_record_filter(),
            ],
            include_embedding=True,
        )
        return hits

    # events ---------------------------------------------------------------------
    def run_event(
        self,
        facts_ops: str,
        *,
        prefs_ops: str = "[]",
        summary: str = "",
        extra_outputs: tuple[str, ...] = (),
        merge: dict[str, Any] | None = None,
        session: str = "s1",
    ) -> dict[str, Any]:
        self.events += 1
        ts = self.now + timedelta(seconds=self.events)
        ev = core.turn_event(ACTOR, session, f"turn {self.events}", "noted", ts)
        key = (
            f"memory/{self.store}/{ACTOR}/session_{session}/events/"
            f"{core.event_key(ts, ev['eventId'][:8])}"
        )
        self.s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(ev).encode())
        # the extractor embeds the rendered transcript; pin it to the "conversation" vector
        # so `candidates()` and the Lambda see the same 8 nearest records
        self.emb.assign(self.h._transcript([ev]), self.emb.embed("conversation"))
        self.bedrock.outputs = [facts_ops, prefs_ops, summary, *extra_outputs]
        body = {
            "kind": "memory_extract",
            "store_id": self.store,
            "workspace_id": DEFAULT_WORKSPACE_ID,
            "actor_id": ACTOR,
            "session_id": session,
            "event_key": key,
            "long_term": True,
            "bucket": BUCKET,
        }
        if merge is not None:
            body["merge"] = merge
        return self.h.process_memory_extract(body, self.deps)

    def hub(self) -> SelfHostedMemoryBackend:
        return SelfHostedMemoryBackend(
            s3=self.s3,
            opensearch=self.os,
            sqs=FakeSQS(),
            embedder=self.emb,
            clock=lambda: self.now,
        )

    def ws(self):
        return ws_ctx(
            {**SUBSTRATE, "memory_id": self.store}, region="cn-northwest-1", id=DEFAULT_WORKSPACE_ID
        )


# ── the generator ──────────────────────────────────────────────────────────────────────

OPS = ("add", "update", "supersede", "delete", "noop")
op_st = st.fixed_dictionaries(
    {
        "op": st.sampled_from(OPS),
        "target": st.integers(
            min_value=0, max_value=CANDIDATE_K + 1
        ),  # ≥ len(cands) → unresolvable
        "sim": st.floats(min_value=0.6, max_value=1.0),
        "confidence": st.one_of(
            st.none(), st.just("absent"), st.floats(min_value=0.0, max_value=1.0)
        ),
        "near": st.booleans(),
        "reuse": st.booleans(),  # take the text of an INACTIVE record (same normalized text)
        "seed": st.integers(min_value=0, max_value=2**31),
    }
)
event_st = st.lists(op_st, max_size=5)
sequence_st = st.lists(event_st, min_size=1, max_size=4)
legacy_seed_st = st.integers(min_value=0, max_value=4)


def _effective_confidence(value: Any, op: str) -> float:
    """Contract: 'an absent value reads as 0 for `delete` only'; a stated value is a float
    in [0, 1]. For the other ops the stored default is the implementation's business."""
    if value is None or value == "absent":
        return 0.0 if op == "delete" else 1.0
    return float(value)


class Oracle:
    """The contract's decision table applied sequentially to a model of the ACTIVE set:
    {record key → vector}. Keys are the record texts (unique per op, so identity by text
    is exact for everything but a deliberate `reuse`)."""

    def __init__(self, actives: dict[str, list[float]]) -> None:
        self.active = dict(actives)
        self.counts = {
            "facts": 0,
            "updates": 0,
            "supersedes": 0,
            "deletes": 0,
            "duplicates": 0,
            "noops": 0,
        }
        self.deleted: set[str] = set()
        self.new_records = 0

    def nearest(self, vector: list[float]) -> tuple[str | None, float]:
        best, best_sim = None, -2.0
        for key, vec in self.active.items():
            # the SAME cosine the fake index computes (N16-obs89b): the oracle's raw unit-vector
            # dot product could land a hair on the other side of the 0.92 dedupe threshold
            # for a generated similarity of exactly 0.92 — a float-boundary flake, not a finding
            sim = cosine(vector, vec)
            if sim > best_sim:
                best, best_sim = key, sim
        return best, best_sim

    def apply(
        self, op: str, target: str | None, confidence: Any, text: str, vector: list[float]
    ) -> None:
        if op == "noop":
            self.counts["noops"] += 1
            return
        if op == "delete":
            if target is None or _effective_confidence(confidence, op) < DELETE_MIN_CONFIDENCE:
                self.counts["noops"] += 1  # "ignored with a job warning (counts.noops)"
                return
            self.active.pop(target, None)
            self.deleted.add(target)
            self.counts["deletes"] += 1
            return
        _nearest, sim = self.nearest(vector)
        if sim >= DEDUPE_COSINE:
            self.counts["duplicates"] += 1
            return
        if op in ("update", "supersede") and target is None:
            op = "add"
        if op == "update":
            # "target's text + embedding rewritten in place: same id" — the live record now
            # reads the new text (keys are texts; the id is the implementation's)
            self.active.pop(target, None)
            self.active[text] = vector
            self.counts["updates"] += 1
            return
        if op == "supersede":
            self.active.pop(target, None)
            self.active[text] = vector
            self.counts["supersedes"] += 1
            self.new_records += 1
            return
        self.active[text] = vector
        self.counts["facts"] += 1
        self.new_records += 1


def _materialize(world: World, ops: list[dict[str, Any]], tag: str, *, restricted: bool):
    """Turn generated ops into the model's JSON answer against the CURRENT candidates and
    pre-compute the oracle's expectations. Returns (json, oracle, aliases)."""
    cands = world.candidates()
    aliases = {f"c{i + 1}": hit for i, hit in enumerate(cands)}
    keyed = {hit["text"]: hit["embedding"] for hit in cands}
    docs = world.docs
    actives = {
        d["text"]: d["embedding"]
        for d in docs.values()
        if d["namespace"] == FACTS and record_is_active(d)
    }
    inactive_texts = sorted(
        d["text"] for d in docs.values() if d["namespace"] == FACTS and not record_is_active(d)
    )
    oracle = Oracle(actives)
    items: list[dict[str, Any]] = []
    used_targets: set[str] = set()
    for n, spec in enumerate(ops):
        op = spec["op"]
        alias = f"c{spec['target'] + 1}"
        target = aliases.get(alias)
        if restricted and target is not None and alias in used_targets:
            target, alias = None, "c99"  # one op per candidate per event
        if target is not None:
            used_targets.add(alias)
        target_key = target["text"] if target is not None else None
        text = f"{tag}-{world.events}-{n}"
        if spec["reuse"] and inactive_texts and not restricted:
            text = inactive_texts[spec["seed"] % len(inactive_texts)]
        existing = list(oracle.active.values()) + list(keyed.values())
        if spec["near"] and target is not None:
            vector = with_cosine(target["embedding"], spec["sim"], spec["seed"])
        elif spec["near"] and oracle.active:
            anchor = list(oracle.active)[spec["seed"] % len(oracle.active)]  # insertion order
            vector = with_cosine(oracle.active[anchor], spec["sim"], spec["seed"])
        else:
            vector = far_unit(existing, spec["seed"])
        if text in world.emb.table and not spec["reuse"]:
            vector = world.emb.table[text]
        world.emb.assign(text, vector)
        item: dict[str, Any] = {"op": op, "target_id": alias if target is not None else None}
        if op != "delete":
            item["text"] = text
        if spec["confidence"] != "absent":
            item["confidence"] = spec["confidence"]
        items.append(item)
        oracle.apply(op, target_key, spec["confidence"], text, vector)
    return json.dumps(items), oracle, aliases


def _check_event(
    world: World, before: dict[str, dict[str, Any]], result: dict[str, Any], oracle: Oracle
) -> None:
    after = world.docs
    counts = result["counts"]
    # (1) job counts == the contract's decision table (BUG-DISCOVERY-PLAN §E06 判据)
    for key in oracle.counts:
        assert counts[key] == oracle.counts[key], (key, counts, oracle.counts)
    # (2) ledger delta == job counts: every add / supersede is a NEW document; nothing else
    # creates one ("new record"), and no document is ever removed (soft delete only).
    assert set(before) <= set(after), "a document was hard-deleted"
    assert len(after) - len(before) == oracle.new_records, (len(before), len(after), counts)
    # (3) delete = soft `deleted_at` on exactly the resolvable, confident targets
    newly_deleted = {
        d["text"]
        for rid, d in after.items()
        if d.get("deleted_at") and not before.get(rid, {}).get("deleted_at")
    }
    assert newly_deleted == oracle.deleted, (newly_deleted, oracle.deleted)
    # (4) history is kept: a superseded / deleted / expired record never becomes active again
    for rid, old in before.items():
        if not record_is_active(old):
            assert not record_is_active(after[rid]), f"inactive record {rid} resurrected"
            for field in ("superseded_by", "deleted_at", "expired_at", "supersedes"):
                if old.get(field):
                    assert after[rid].get(field) == old[field], (rid, field)
    # (5) the active set the oracle predicts (by text) is the active set in the index
    live = {d["text"] for d in after.values() if d["namespace"] == FACTS and record_is_active(d)}
    assert live == set(oracle.active), (live, set(oracle.active))
    # (6) structural invariants: acyclic chains, consistent `supersedes`, no ≥0.92 pair
    assert_memory_index_invariants(after)


def _check_recall(world: World, top_k: int = 3) -> None:
    """memory-backend.md "Recall": only ACTIVE records, ranked by cosine × 0.5^(age/half-life)
    × max(confidence, 0.5) over an over-fetch of min(4 × top_k, 20); `score` is that value
    and `similarity` the raw cosine."""
    hub = world.hub()
    out = hub.search_records(world.ws(), FACTS, "conversation", top_k=top_k)
    items = out["items"]
    docs = world.docs
    live = active_ids(docs)
    assert all(i["active"] and i["record_id"] in live for i in items), items
    assert len(items) <= top_k
    query = world.emb.embed("conversation")
    fetch = max(min(RECALL_OVERFETCH[0] * top_k, RECALL_OVERFETCH[1]), top_k)
    pool = sorted(
        ((rid, d) for rid, d in docs.items() if d["namespace"] == FACTS and rid in live),
        key=lambda item: _cos(query, item[1]["embedding"]),
        reverse=True,
    )[:fetch]
    expected: list[tuple[float, str]] = []
    for rid, d in pool:
        stamp = datetime.fromisoformat(d.get("updated_at") or d["created_at"])
        age = (world.now - stamp).total_seconds() / 86400
        conf = 1.0 if d.get("confidence") is None else float(d["confidence"])
        score = (
            _cos(query, d["embedding"])
            * (0.5 ** (age / RECALL_HALF_LIFE_DEFAULT))
            * max(conf, RECALL_FLOOR)
        )
        expected.append((score, rid))
    expected.sort(key=lambda t: t[0], reverse=True)
    got = [(i["score"], i["record_id"]) for i in items]
    assert [r for _, r in got] == [r for _, r in expected[:top_k]] or (
        # ties may reorder — then the score multiset must agree
        sorted(round(s, 9) for s, _ in got) == sorted(round(s, 9) for s, _ in expected[:top_k])
    ), (got, expected[:top_k])
    for (score, _rid), item in zip(expected[:top_k], items, strict=False):
        assert item["score"] == pytest.approx(score, abs=1e-9)
        assert item["similarity"] == pytest.approx(
            _cos(query, docs[item["record_id"]]["embedding"]), abs=1e-9
        )
    assert [s for s, _ in got] == sorted((s for s, _ in got), reverse=True)


def _seed_legacy(world: World, n: int) -> list[str]:
    vectors: list[list[float]] = []
    ids: list[str] = []
    for i in range(n):
        v = far_unit(vectors, 1000 + i)
        vectors.append(v)
        ids.append(
            world.seed(
                None, f"legacy fact {i}", v, legacy=True, created=T0 - timedelta(days=30 * (i + 1))
            )["_id"]
        )
    return ids


def _run_sequence(handler, legacy: int, sequence, *, restricted: bool) -> World:
    world = World(handler)
    _seed_legacy(world, legacy)
    for ops in sequence:
        if restricted:
            # one op per candidate and no text reuse: the shapes under which the decision
            # table is well-defined without ordering effects inside one event
            seen: set[int] = set()
            ops = [o for o in ops if not (o["target"] in seen or seen.add(o["target"]))]
        payload, oracle, _aliases = _materialize(world, ops, "fact", restricted=restricted)
        before = world.docs
        result = world.run_event(payload)
        assert result["status"] == "COMPLETED", result
        _check_event(world, before, result, oracle)
        _check_recall(world)
        world.now = world.now + timedelta(hours=1)
    return world


# ── properties: the decision table over random sequences ───────────────────────────────


@HYP
@given(legacy=legacy_seed_st, sequence=sequence_st)
def test_restricted_sequences_hold_every_invariant(handler, legacy, sequence):
    """One operation per candidate per event, no text reuse: the contract's table, the
    ledger delta, soft-delete / history, chains, dedupe and recall all hold."""
    _run_sequence(handler, legacy, sequence, restricted=True)


@pytest.mark.xfail(
    # strict: HYP_XFAIL generates deterministically (derandomize), so the 40 examples are the
    # same on every run and reach the resurrection every time (an XPASS under the 32-worker
    # chain gate came from randomized generation, r16-k merge)
    strict=True,
    reason="N16-obs30: an `update` (or `add`/`supersede` whose normalized text matches) "
    "overwrites a record from a pre-event candidate snapshot or by id, resurrecting a "
    "superseded / deleted record and breaking the supersedes ↔ superseded_by pairing",
)
@HYP_XFAIL
@given(legacy=legacy_seed_st, sequence=sequence_st)
def test_unrestricted_sequences_hold_every_invariant(handler, legacy, sequence):
    """Any model answer the contract admits (several ops on one candidate, a text equal
    to a retired record's) must still land in a contract-conforming index."""
    _run_sequence(handler, legacy, sequence, restricted=False)


# ── shrunk repros of the unrestricted failures (deterministic) ─────────────────────────


def _paris_london(world: World) -> str:
    paris = rand_unit("paris")
    return world.seed(None, "lives in Paris", paris, created=T0 - timedelta(days=10))["_id"]


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs30 (a): supersede → supersede back with the "
    "original wording resurrects the retired record and leaves a cycle",
)
def test_supersede_back_to_the_original_text_resurrects_history(handler):
    """memory-backend.md: "Superseded records are kept forever as history"; the chain
    Paris → London → Paris must stay acyclic and every `supersedes` member inactive."""
    world = World(handler)
    paris = _paris_london(world)
    london = with_cosine(world.emb.embed("lives in Paris"), 0.7, 1)
    world.emb.assign("moved to London", london)
    world.run_event(
        json.dumps(
            [{"op": "supersede", "target_id": "c1", "confidence": 0.9, "text": "moved to London"}]
        )
    )
    assert not record_is_active(world.docs[paris])
    # the user moves back: the model supersedes London with the ORIGINAL sentence
    world.run_event(
        json.dumps(
            [{"op": "supersede", "target_id": "c1", "confidence": 0.9, "text": "lives in Paris"}]
        )
    )
    violations = scan_memory_index(world.docs)
    assert violations == [], violations


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs30 (b): delete then update of the same "
    "candidate in one event rewrites the tombstone away (the update works "
    "from the pre-event snapshot)",
)
def test_delete_then_update_of_one_candidate_keeps_the_tombstone(handler):
    """Contract: delete = soft `deleted_at`, "never a hard delete"; an update "rewrites text
    + embedding in place" — it does not un-delete."""
    world = World(handler)
    paris = _paris_london(world)
    world.emb.assign("lives in Paris, 11e", with_cosine(world.emb.embed("lives in Paris"), 0.8, 2))
    result = world.run_event(
        json.dumps(
            [
                {"op": "delete", "target_id": "c1", "confidence": 0.95},
                {
                    "op": "update",
                    "target_id": "c1",
                    "confidence": 0.9,
                    "text": "lives in Paris, 11e",
                },
            ]
        )
    )
    assert result["counts"]["deletes"] == 1
    assert world.docs[paris].get("deleted_at"), "the delete was undone by the update"


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs30 (c): supersede then update of the same "
    "candidate in one event drops `superseded_by` → two ACTIVE records, the "
    "new one claiming to supersede a live record",
)
def test_supersede_then_update_of_one_candidate_keeps_the_target_retired(handler):
    world = World(handler)
    _paris_london(world)
    base = world.emb.embed("lives in Paris")
    world.emb.assign("moved to London", with_cosine(base, 0.7, 1))
    world.emb.assign("lives in Paris, 11e", with_cosine(base, 0.8, 2))
    world.run_event(
        json.dumps(
            [
                {
                    "op": "supersede",
                    "target_id": "c1",
                    "confidence": 0.9,
                    "text": "moved to London",
                },
                {
                    "op": "update",
                    "target_id": "c1",
                    "confidence": 0.9,
                    "text": "lives in Paris, 11e",
                },
            ]
        )
    )
    violations = scan_memory_index(world.docs)
    assert violations == [], violations


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs30 (d): an `add` whose normalized text equals "
    "a soft-deleted record's overwrites the tombstone by id (history lost)",
)
def test_add_of_a_deleted_records_text_does_not_revive_it(handler):
    world = World(handler)
    paris = _paris_london(world)
    world.run_event(json.dumps([{"op": "delete", "target_id": "c1", "confidence": 0.9}]))
    before = world.docs
    assert before[paris].get("deleted_at")
    # a later conversation states the same fact again; the model (seeing no active
    # candidate) answers `add` — a new record; the retired one must stay retired
    result = world.run_event(
        json.dumps([{"op": "add", "target_id": None, "confidence": 0.9, "text": "lives in Paris"}])
    )
    after = world.docs
    assert result["counts"]["facts"] == 1
    assert len(after) == len(before) + 1, "the add reused the tombstone's document"
    assert after[paris].get("deleted_at") == before[paris]["deleted_at"]


# ── metamorphic: paraphrase (same embedding, other wording) → same op class ────────────


@HYP
@given(legacy=legacy_seed_st, sequence=sequence_st)
def test_paraphrase_equivalent_sequences_take_the_same_operations(handler, legacy, sequence):
    """Contract: every decision is made on the embedding (cosine) and the model's op —
    renaming every new fact's wording while keeping its vector must yield identical job
    counts and the same number of active / retired records."""
    worlds = []
    for tag in ("fact", "paraphrase"):
        world = World(handler)
        _seed_legacy(world, legacy)
        trace = []
        for ops in sequence:
            seen: set[int] = set()
            ops = [o for o in ops if not (o["target"] in seen or seen.add(o["target"]))]
            payload, _oracle, _ = _materialize(world, ops, tag, restricted=True)
            trace.append(world.run_event(payload)["counts"])
            world.now = world.now + timedelta(hours=1)
        worlds.append((world, trace))
    (a, trace_a), (b, trace_b) = worlds
    assert trace_a == trace_b
    shape = lambda w: sorted(  # noqa: E731
        (
            record_is_active(d),
            bool(d.get("deleted_at")),
            bool(d.get("superseded_by")),
            len(d.get("supersedes") or []),
            d.get("last_op") or "",
        )
        for d in w.docs.values()
    )
    assert shape(a) == shape(b)


# ── delete gate: resolvable target AND stated confidence ≥ 0.6 ───────────────────────


@HYP_LIGHT
@given(
    confidence=st.one_of(
        st.none(),
        st.just("absent"),
        st.floats(min_value=0.0, max_value=1.0),
        st.sampled_from(["0.9", "high", ""]),
    ),
    resolvable=st.booleans(),
)
def test_delete_is_honoured_only_with_a_candidate_and_confidence_at_least_point_six(
    handler,
    confidence,
    resolvable,
):
    world = World(handler)
    world.seed("r1", "likes tea", rand_unit("tea"))
    item: dict[str, Any] = {"op": "delete", "target_id": "c1" if resolvable else "c7"}
    if confidence != "absent":
        item["confidence"] = confidence
    result = world.run_event(json.dumps([item]))
    try:
        stated = float(confidence) if confidence not in (None, "absent") else 0.0
    except (TypeError, ValueError):
        stated = 0.0  # an unparseable confidence is not a stated one
    honoured = resolvable and stated >= DELETE_MIN_CONFIDENCE
    assert bool(world.docs["r1"].get("deleted_at")) is honoured, (confidence, resolvable, result)
    assert result["counts"]["deletes"] == int(honoured)
    assert result["counts"]["noops"] == int(not honoured)
    if not honoured:  # "ignored with a job warning"
        (job,) = [j for j in world.os.indices[JOBS].values() if j.get("kind") == "memory_extract"]
        assert any("delete" in w for w in job["warnings"]), job["warnings"]


# ── pre-v2 (legacy) documents ─────────────────────────────────────────────────────────


@HYP_LIGHT
@given(n=st.integers(min_value=1, max_value=6), k=st.integers(min_value=1, max_value=6))
def test_legacy_documents_read_as_active_with_confidence_one(handler, n, k):
    """Contract: "A document without them is ACTIVE with confidence 1.0 and timestamp
    `created_at`" — the hub lists them, recall scores them at the unweighted cosine ×
    recency and the extractor offers them as candidates."""
    world = World(handler)
    ids = _seed_legacy(world, n)
    hub = world.hub()
    listed = hub.list_records(world.ws(), FACTS)["items"]
    assert {r["record_id"] for r in listed} == set(ids)
    assert all(r["active"] and r["confidence"] == 1.0 for r in listed)
    _check_recall(world, top_k=k)
    assert len(world.candidates()) == min(n, CANDIDATE_K)


# ── caps and consolidation ─────────────────────────────────────────────────────────────


def _seed_distinct(world: World, n: int, *, prefix: str = "f") -> list[str]:
    vectors: list[list[float]] = []
    ids = []
    for i in range(n):
        v = far_unit(vectors, 5000 + i, ceiling=0.5)
        vectors.append(v)
        ids.append(
            world.seed(None, f"{prefix} fact {i}", v, created=T0 - timedelta(days=n - i))["_id"]
        )
    return ids


groups_st = st.lists(
    st.fixed_dictionaries(
        {
            "members": st.lists(
                st.integers(min_value=1, max_value=24), min_size=0, max_size=4, unique=True
            ),
            "unknown": st.booleans(),
            "confidence": st.floats(min_value=0.0, max_value=1.0),
        }
    ),
    max_size=6,
)


@HYP
@given(total=st.integers(min_value=CAP_MIN + 1, max_value=CAP_MIN + 30), groups=groups_st)
def test_consolidation_pass_is_bounded_and_ignores_single_unknown_and_reused_ids(
    handler,
    total,
    groups,
):
    """Contract "Cap and re-summarization": over the cap, ONE pass takes the oldest
    min(20, max(4, 2 × overflow)) records; each group of two or more becomes one
    `consolidate` record superseding its members; single-member groups, unknown ids and
    reused ids are ignored."""
    world = World(handler)
    ids = _seed_distinct(world, total)
    overflow = total - CAP_MIN
    take = min(CONSOLIDATE_MAX, max(4, 2 * overflow), total)
    listed = ids[:take]  # oldest first (created_at ascending) → aliases r1..r_take
    used: set[int] = set()
    expected_groups: list[list[str]] = []
    answer = []
    for g, spec in enumerate(groups):
        aliases = [f"r{m}" for m in spec["members"]]
        if spec["unknown"]:
            aliases.append("r99")
        text = f"merged-{g}"
        world.emb.assign(
            text, far_unit([world.emb.embed(f"f fact {i}") for i in range(total)], 9000 + g)
        )
        answer.append({"text": text, "merges": aliases, "confidence": spec["confidence"]})
        valid = [m for m in dict.fromkeys(spec["members"]) if 1 <= m <= take and m not in used]
        if len(valid) >= 2:
            used.update(valid)
            expected_groups.append([listed[m - 1] for m in valid])
    before = world.docs
    result = world.run_event(
        "[]", extra_outputs=(json.dumps(answer),), merge={"max_facts_per_actor": CAP_MIN}
    )
    after = world.docs
    assert result["counts"]["consolidated"] == sum(len(g) for g in expected_groups)
    new = {rid: d for rid, d in after.items() if rid not in before}
    assert len(new) == len(expected_groups)
    for members in expected_groups:
        (parent,) = [
            rid for rid, d in new.items() if sorted(d.get("supersedes") or []) == sorted(members)
        ]
        assert new[parent]["last_op"] == "consolidate" and record_is_active(new[parent])
        for m in members:
            assert after[m]["superseded_by"] == parent and not record_is_active(after[m])
    untouched = set(ids) - {m for g in expected_groups for m in g}
    assert all(record_is_active(after[rid]) for rid in untouched)
    assert len(active_ids(after)) == total - sum(len(g) for g in expected_groups) + len(
        expected_groups
    )
    assert_memory_index_invariants(after)
    # exactly one consolidation prompt per namespace per event (bounded pass)
    prompts = [c["system"][0]["text"] for c in world.bedrock.converse_calls]
    assert sum("grown past its size limit" in p for p in prompts) == 1


@HYP
@given(total=st.integers(min_value=CAP_MIN + 1, max_value=CAP_MIN + 25))
def test_a_namespace_over_its_cap_converges_over_several_extractions(handler, total):
    """Contract: "The pass is bounded to one per event, so a namespace far over its cap
    converges over several extractions" — with a model that always pairs what it is shown,
    the active count decreases every event until it is ≤ cap, and never below the cap by
    more than one pass can remove."""
    world = World(handler)
    _seed_distinct(world, total)
    active = len(active_ids(world.docs))
    passes = 0
    while active > CAP_MIN:
        overflow = active - CAP_MIN
        take = min(CONSOLIDATE_MAX, max(4, 2 * overflow), active)
        pairs = [
            {"text": f"pair-{passes}-{i}", "merges": [f"r{i}", f"r{i + 1}"], "confidence": 0.9}
            for i in range(1, take, 2)
        ]
        for p in pairs:
            world.emb.assign(
                p["text"],
                far_unit([d["embedding"] for d in world.docs.values()], hash(p["text"]) % 10**6),
            )
        world.run_event(
            "[]", extra_outputs=(json.dumps(pairs),), merge={"max_facts_per_actor": CAP_MIN}
        )
        world.now += timedelta(minutes=1)
        passes += 1
        now_active = len(active_ids(world.docs))
        assert now_active == active - len(pairs), (active, now_active, len(pairs))
        active = now_active
        assert passes <= total, "did not converge"
    assert active <= CAP_MIN
    assert_memory_index_invariants(world.docs)
    # under the cap: no consolidation prompt is sent any more
    calls = len(world.bedrock.converse_calls)
    world.run_event("[]", merge={"max_facts_per_actor": CAP_MIN})
    prompts = [c["system"][0]["text"] for c in world.bedrock.converse_calls[calls:]]
    assert not any("grown past its size limit" in p for p in prompts)


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs33: a group that repeats one alias "
    "(`merges: [r1, r1]`) passes the two-or-more check — a single-member group "
    "is consolidated, `supersedes` lists the member twice and `consolidated` "
    "counts 2",
)
def test_consolidation_ignores_a_group_that_repeats_one_alias(handler):
    """Contract "Cap and re-summarization": "Single-member groups, unknown ids and reused ids
    are ignored" — a group naming the same record twice has one member."""
    world = World(handler)
    ids = _seed_distinct(world, CAP_MIN + 2)
    world.emb.assign(
        "merged-dup", far_unit([world.emb.embed(f"f fact {i}") for i in range(CAP_MIN + 2)], 42)
    )
    before = world.docs
    result = world.run_event(
        "[]",
        extra_outputs=(
            json.dumps([{"text": "merged-dup", "merges": ["r1", "r1"], "confidence": 0.9}]),
        ),
        merge={"max_facts_per_actor": CAP_MIN},
    )
    assert result["counts"]["consolidated"] == 0, result["counts"]
    assert world.docs.keys() == before.keys()
    assert record_is_active(world.docs[ids[0]])


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs31: a consolidation whose merged text equals a "
    "member's text makes the member supersede ITSELF (id collision on the "
    "normalized text) — a one-node cycle",
)
def test_consolidation_never_makes_a_record_supersede_itself(handler):
    world = World(handler)
    ids = _seed_distinct(world, CAP_MIN + 2)
    answer = [{"text": "f fact 0", "merges": ["r1", "r2"], "confidence": 0.9}]  # keeps r1's wording
    world.run_event(
        "[]", extra_outputs=(json.dumps(answer),), merge={"max_facts_per_actor": CAP_MIN}
    )
    violations = scan_memory_index(world.docs)
    assert violations == [], violations
    assert ids[0] not in (world.docs[ids[0]].get("supersedes") or [])


# ── soft TTL ──────────────────────────────────────────────────────────────────────────


@HYP
@given(
    ttl=st.integers(min_value=TTL_BOUNDS[0], max_value=TTL_BOUNDS[1]),
    ages=st.lists(st.integers(min_value=0, max_value=5000), min_size=1, max_size=60, unique=True),
    bumped=st.data(),
)
def test_ttl_marks_at_most_fifty_stale_records_per_event_hides_them_and_never_deletes(
    handler,
    ttl,
    ages,
    bumped,
):
    """Contract "TTL": the hub hides records older than the TTL (by `updated_at`, else
    `created_at`) with no write; the extractor marks ≤ 50 of them `expired_at` per event;
    the actor summary is never expired; nothing is deleted."""
    world = World(handler)
    hub = world.hub()
    created = hub.create_memory_resource(
        world.ws(),
        f"ttl-{next(_STORE_SEQ)}",
        "",
        30,
        ["semantic"],
        merge={"long_term_ttl_days": ttl},
    )
    world.store = created["id"]
    vectors: list[list[float]] = []
    stale: list[tuple[str, int]] = []
    facts: set[str] = set()
    for i, age in enumerate(ages):
        v = far_unit(vectors, 7000 + i, ceiling=0.5)
        vectors.append(v)
        bump = bumped.draw(st.one_of(st.none(), st.integers(min_value=0, max_value=age)))
        rid = world.seed(
            None,
            f"ttl fact {i}",
            v,
            created=T0 - timedelta(days=age),
            updated=(T0 - timedelta(days=bump)) if bump is not None else None,
            legacy=bump is None,
        )["_id"]
        facts.add(rid)
        effective = bump if bump is not None else age
        if effective > ttl:
            stale.append((rid, age))
    # the rolling actor summary: ancient, and never expired
    world.seed(
        "actor-summary",
        json.dumps({"summary": "old"}),
        far_unit(vectors, 1),
        namespace=f"/summaries/{ACTOR}/_actor",
        strategy="summarization",
        created=T0 - timedelta(days=4000),
        metadata={"kind": "actor_summary"},
    )
    before = world.docs
    listed = hub.list_records(world.ws(), FACTS, max_results=100)["items"]
    assert {r["record_id"] for r in listed} == facts - {s for s, _ in stale}
    result = world.run_event("[]", merge={"long_term_ttl_days": ttl})
    after = world.docs
    assert set(after) == set(before), "TTL deleted a document"
    expected = {rid for rid, _ in sorted(stale, key=lambda t: -t[1])[:EXPIRE_MAX_PER_EVENT]}
    expired = {rid for rid, d in after.items() if d.get("expired_at")}
    assert expired == expected, (len(expired), len(expected), len(stale))
    assert result["counts"]["expired"] == len(expected)
    assert not after["actor-summary"].get("expired_at")
    listed_after = hub.list_records(world.ws(), FACTS, max_results=100)["items"]
    assert {r["record_id"] for r in listed_after} == {r["record_id"] for r in listed}
    assert_memory_index_invariants(after)


# ── seam faults: real OpenSearch / urllib3 error shapes on the hub paths ──────────────


class FaultyOpenSearch:
    """`FakeOpenSearch` whose named methods raise the exact shapes the hub-side funnel
    surfaces: `OpenSearchError(status, body)` for a non-2xx answer (opensearch_http.request)
    and the urllib3 exception `RetryingHttp` re-raises on its last attempt (N14-obs4)."""

    def __init__(self, inner: FakeOpenSearch, faults: dict[str, BaseException]) -> None:
        self.inner = inner
        self.faults = faults
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if name in self.faults:

            def boom(*args: Any, **kwargs: Any) -> Any:
                self.calls.append(name)
                raise self.faults[name]

            return boom
        return attr


BODY_MARKER = "do-not-echo-record-text"
REAL_FAULTS: dict[str, BaseException] = {
    # OpenSearch data plane while throttling (es_rejected_execution_exception, HTTP 429)
    "throttle_429": core.OpenSearchError(
        429,
        '{"error":{"root_cause":[{"type":"es_rejected_execution_exception","reason":'
        '"rejected execution of … queue capacity = 1000 ' + BODY_MARKER + '"}]},"status":429}',
    ),
    # the domain with no master / red cluster (HTTP 503)
    "unavailable_503": core.OpenSearchError(
        503,
        '{"error":{"root_cause":[{"type":"cluster_block_exception","reason":"index '
        "[launchpad-memory] blocked by: [SERVICE_UNAVAILABLE/2/no master] "
        + BODY_MARKER
        + '"}]},"status":503}',
    ),
    # the domain endpoint's front door answering for a dead node (HTML 502)
    "bad_gateway_502": core.OpenSearchError(
        502, "<html><body>502 Bad Gateway " + BODY_MARKER + "</body></html>"
    ),
    # N14-obs4 live shape: the retrying pool's last attempt timed out (interactive tier 10 s)
    "read_timeout": urllib3.exceptions.ReadTimeoutError(
        None, "/launchpad-memory/_search", "Read timed out. (read timeout=10.0)"
    ),
}
# memory-backend.md "Error codes": 429 / 5xx → 503 `memory.backend_unavailable`; a transport
# failure the pool gave up on reaches the router's `_guard` → 502 `memory.unavailable`
# (routers/memory.py; N14-obs4).
EXPECTED_CODE = {
    "throttle_429": (503, "memory.backend_unavailable"),
    "unavailable_503": (503, "memory.backend_unavailable"),
    "bad_gateway_502": (503, "memory.backend_unavailable"),
    "read_timeout": (502, "memory.unavailable"),
}


def _api_world(handler, faults: dict[str, BaseException]) -> tuple[World, FaultyOpenSearch]:
    world = World(handler)
    world.seed("r-tea", "likes tea", rand_unit("tea"))
    faulty = FaultyOpenSearch(world.os, faults)
    memory_backend.set_backend(
        SelfHostedMemoryBackend(
            s3=world.s3,
            opensearch=faulty,
            sqs=FakeSQS(),
            embedder=world.emb,
            clock=lambda: T0,
        )
    )
    set_default_resources(SUBSTRATE)
    return world, faulty


def _assert_envelope(res, status: int, code: str) -> None:
    assert res.status_code == status, res.text
    body = res.json()
    assert body["code"] == code, body
    assert "Traceback" not in res.text and BODY_MARKER not in res.text, res.text


@pytest.mark.parametrize("fault", sorted(REAL_FAULTS))
@pytest.mark.parametrize("route", ["search", "list", "delete_get", "delete_delete"])
def test_hub_read_and_delete_paths_map_real_opensearch_faults_to_the_contract_codes(
    client,
    handler,
    fault,
    route,
):
    method = {
        "search": "knn_search",
        "list": "search",
        "delete_get": "get_doc",
        "delete_delete": "delete_doc",
    }[route]
    _world, faulty = _api_world(handler, {method: REAL_FAULTS[fault]})
    if route == "search":
        res = client.post("/api/memory/records/search", json={"namespace": FACTS, "query": "tea"})
    elif route == "list":
        res = client.get("/api/memory/records", params={"namespace": FACTS})
    else:
        res = client.delete("/api/memory/records/r-tea", params={"actor_id": ACTOR})
    status, code = EXPECTED_CODE[fault]
    _assert_envelope(res, status, code)
    assert faulty.calls == [method]
    if route.startswith("delete"):
        assert "r-tea" in faulty.inner.indices[MEM], "a failed delete must not remove the record"


@pytest.mark.parametrize("fault", sorted(REAL_FAULTS))
def test_store_delete_answers_200_when_only_the_purge_fails(client, handler, fault):
    """N14-obs4 / memory-backend.md: the delete is complete once the ledger row is gone;
    the OpenSearch purge is best-effort for every fault shape (HTTP and transport)."""
    _world, _faulty = _api_world(handler, {"delete_by_query": REAL_FAULTS[fault]})
    created = client.post(
        "/api/memory/resources", json={"name": "to_purge", "strategies": ["semantic"]}
    )
    assert created.status_code == 201, created.text
    sid = created.json()["id"]
    res = client.delete(f"/api/memory/resources/{sid}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["id"] == sid and body["deleted"] is True
    assert body["cleanup"] == {"s3": "ok", "index": "skipped"}
    assert body["cleanup_warnings"][0].startswith("index: ")  # type + status / code only
    assert BODY_MARKER not in res.text
    assert client.get(f"/api/memory/resources/{sid}").status_code == 404


def test_record_delete_of_a_vanished_document_is_the_contract_404(client, handler):
    """memory-backend.md `delete_record`: a record that is gone (or another actor's) answers
    404 `memory.not_found` and nothing is removed."""
    world, _faulty = _api_world(handler, {})
    res = client.delete("/api/memory/records/r-gone", params={"actor_id": ACTOR})
    _assert_envelope(res, 404, "memory.not_found")
    res = client.delete("/api/memory/records/r-tea", params={"actor_id": "agent2__other"})
    _assert_envelope(res, 404, "memory.not_found")
    assert "r-tea" in world.docs


# ── seam faults inside the extractor ──────────────────────────────────────────────────


@pytest.mark.parametrize("fault", ["throttle_429", "unavailable_503", "read_timeout"])
def test_extractor_reports_a_transient_search_fault_as_retryable_and_records_the_job(
    handler,
    fault,
):
    """handler.py `RetryableError`: "Transient failure — let SQS redeliver"; the job row
    carries the failure (type only, never the body)."""
    world = World(handler)
    world.seed("r-tea", "likes tea", rand_unit("tea"))
    world.deps.opensearch = FaultyOpenSearch(world.os, {"knn_search": REAL_FAULTS[fault]})
    with pytest.raises(handler.RetryableError):
        world.run_event(json.dumps([{"op": "add", "text": "new fact", "confidence": 0.9}]))
    jobs = [j for j in world.os.indices[JOBS].values() if j.get("kind") == "memory_extract"]
    assert len(jobs) == 1 and jobs[0]["status"] == "FAILED"
    assert jobs[0]["failure_reason"].startswith(type(REAL_FAULTS[fault]).__name__)
    assert "r-tea" in world.docs and len(world.docs) == 1  # nothing half-written


def _supersede_world(handler) -> World:
    world = World(handler)
    world.seed("r-tea", "likes tea", rand_unit("tea"))
    world.emb.assign("switched to coffee", with_cosine(rand_unit("tea"), 0.7, 200))
    return world


def _supersede_ops() -> str:
    return json.dumps([{"op": "supersede", "target_id": "c1", "text": "switched to coffee",
                        "confidence": 0.9}])


def test_supersede_writes_the_successor_and_retires_the_target_in_one_bulk_request(handler):
    """N17-obs90 (fixed in cn/r17-n): the supersede pair travels in ONE `bulk` request —
    no `put_doc` / `update_doc` on the memory index between which a fault could land."""
    world = _supersede_world(handler)
    calls: list[tuple[str, str]] = []

    class Recording:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            attr = getattr(self.inner, name)
            if name in ("put_doc", "update_doc", "bulk"):

                def record(index, *a, **k):
                    calls.append((name, index))
                    return attr(index, *a, **k)

                return record
            return attr

    world.deps.opensearch = Recording(world.os)
    result = world.run_event(_supersede_ops())
    assert result["counts"]["supersedes"] == 1, result
    memory_writes = [c for c in calls if c[1] == MEM]
    assert memory_writes == [("bulk", MEM)], calls
    docs = world.docs
    (new_id,) = [rid for rid in docs if rid != "r-tea"]
    assert docs["r-tea"]["superseded_by"] == new_id and docs["r-tea"]["superseded_at"]
    assert docs["r-tea"]["embedding"] == rand_unit("tea")  # the retired copy is lossless
    assert docs[new_id]["supersedes"] == ["r-tea"]
    assert_memory_index_invariants(docs)


@pytest.mark.parametrize("target", ["original", "successor", "none"])
def test_replay_after_a_half_written_supersede_converges_to_one_active_record(handler, target):
    """N17-obs122 (covered by cn/r17-n): the index holds a half-written pair — the successor
    (`supersedes=[original]`) and the ORIGINAL both active (the pre-fix two-write supersede
    interrupted between its writes, or a partial bulk). The SQS replay of the same op dedupes
    against the successor (nearest at cosine 1.0) whatever the model names — the original
    (`c1`), the successor itself (the self-supersede guard) or nothing — and used to leave the
    original active for good. Now the duplicate branch finishes the pair: the original is
    retired under the successor, exactly one record is active, the scan is clean."""
    world = _supersede_world(handler)
    successor_id = handler._record_id(STORE, FACTS, "switched to coffee")
    world.seed(successor_id, "switched to coffee", world.emb.embed("switched to coffee"),
               last_op="supersede", supersedes=["r-tea"])
    assert world.docs["r-tea"].get("superseded_by") is None  # the half-written pair
    assert first_violation(scan_memory_index(world.docs), "carries no superseded_by")
    # candidates are ordered by similarity to the transcript — pin which alias is which
    cands = world.candidates()
    by_id = {c["_id"]: f"c{i + 1}" for i, c in enumerate(cands)}
    alias = {"original": by_id["r-tea"], "successor": by_id[successor_id], "none": None}[target]
    result = world.run_event(json.dumps([{"op": "supersede", "target_id": alias,
                                          "text": "switched to coffee", "confidence": 0.9}]))
    assert result["status"] == "COMPLETED"
    assert result["counts"]["duplicates"] == 1 and result["counts"]["supersedes"] == 1, result
    docs = world.docs
    assert set(docs) == {"r-tea", successor_id}
    assert docs["r-tea"]["superseded_by"] == successor_id and docs["r-tea"]["superseded_at"]
    assert [rid for rid, d in docs.items() if record_is_active(d)] == [successor_id]
    assert_memory_index_invariants(docs)


def test_the_duplicate_rule_still_writes_nothing_for_an_unrelated_near_duplicate(handler):
    """The obs122 heal touches only a record the successor already CLAIMS: a near-duplicate of
    an unrelated active record keeps the table's 'duplicate, nothing written, whatever the op'."""
    world = World(handler)
    world.seed("r-tea", "likes tea", rand_unit("tea"))
    world.seed("r-city", "lives in Xi'an", rand_unit("city"))
    world.emb.assign("enjoys a cup of tea", with_cosine(rand_unit("tea"), 0.95, 9))
    cands = world.candidates()
    city_alias = {c["_id"]: f"c{i + 1}" for i, c in enumerate(cands)}["r-city"]
    before = world.docs
    result = world.run_event(json.dumps([{"op": "supersede", "target_id": city_alias,
                                          "text": "enjoys a cup of tea", "confidence": 0.9}]))
    assert result["counts"]["duplicates"] == 1 and result["counts"]["supersedes"] == 0
    assert world.docs == before


@pytest.mark.parametrize("fault", ["throttle_429", "unavailable_503", "read_timeout"])
def test_a_faulted_supersede_leaves_the_index_untouched_and_retries(handler, fault):
    """N17-obs90: a fault on the one bulk write leaves NEITHER half — no active successor
    naming an un-retired target — and the redelivery (seam healthy) applies the op."""
    world = _supersede_world(handler)
    before = world.docs
    world.deps.opensearch = FaultyOpenSearch(world.os, {"bulk": REAL_FAULTS[fault]})
    with pytest.raises(handler.RetryableError):
        world.run_event(_supersede_ops())
    assert world.docs == before
    jobs = [j for j in world.os.indices[JOBS].values() if j.get("kind") == "memory_extract"]
    assert [j["status"] for j in jobs] == ["FAILED"]
    world.deps.opensearch = world.os
    world.events -= 1  # the same event again
    result = world.run_event(_supersede_ops())
    assert result["status"] == "COMPLETED" and result["counts"]["supersedes"] == 1
    assert_memory_index_invariants(world.docs)
    assert not {rid for rid, d in world.docs.items() if d.get("superseded_by")} - {"r-tea"}


class StaleUntilRefresh:
    """OpenSearch's near-real-time reads: k-NN / search see nothing until a `_refresh` request
    has been issued (a write with `refresh=False` sits in an unrefreshed segment)."""

    def __init__(self, inner: FakeOpenSearch) -> None:
        self.inner = inner
        self.refreshed = False
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if name == "request":

            def request(method, path, *a, **k):
                if method == "POST" and path.endswith("/_refresh"):
                    self.calls.append("_refresh")
                    self.refreshed = True
                return attr(method, path, *a, **k)

            return request
        if name == "knn_search":

            def knn_search(*a, **k):
                self.calls.append("knn_search")
                return attr(*a, **k) if self.refreshed else []

            return knn_search
        return attr


def test_the_dedupe_check_is_a_strong_read_after_a_refresh(handler):
    """N17-obs101 (backend part, cn/r17-n): a record another extraction wrote seconds ago is
    invisible to the candidate read; the extractor refreshes right before its nearest-record
    re-check, so the near-duplicate (cosine 0.95) is counted as a duplicate and nothing is
    written — the merge-v2 invariant 'no two ACTIVE records at cosine ≥ 0.92' holds."""
    world = World(handler)
    world.seed("r-tea", "likes tea", rand_unit("tea"))
    world.emb.assign("enjoys a cup of tea", with_cosine(rand_unit("tea"), 0.95, 7))
    stale = StaleUntilRefresh(world.os)
    world.deps.opensearch = stale
    result = world.run_event(json.dumps([{"op": "add", "text": "enjoys a cup of tea",
                                          "confidence": 0.9}]))
    assert result["counts"]["duplicates"] == 1 and result["counts"]["facts"] == 0, result
    assert set(world.docs) == {"r-tea"}
    # Both strategies' candidates are read before core mutations. The refresh still comes
    # immediately BEFORE the nearest-record re-check, which must see the concurrent write.
    assert stale.calls[:4] == [
        "knn_search", "knn_search", "_refresh", "knn_search",
    ], stale.calls
    assert_memory_index_invariants(world.docs)


@pytest.mark.parametrize("fault", ["throttle_429", "unavailable_503", "read_timeout"])
def test_extractor_treats_a_domain_wide_transient_fault_as_retryable(handler, fault):
    """N16-obs32 (fixed in cn/r17-n): when the jobs index itself answers 429 / 503 or times out
    (domain-wide throttle / no master) the bookkeeping before the work is a `RetryableError`
    and the FAILED write after it is fail-soft — never the raw exception `lambda_handler`
    used to ack as poison."""
    world = World(handler)
    exc = REAL_FAULTS[fault]
    world.deps.opensearch = FaultyOpenSearch(
        world.os,
        {m: exc for m in ("get_doc", "put_doc", "update_doc", "knn_search", "search", "request")},
    )
    with pytest.raises(handler.RetryableError):
        world.run_event(json.dumps([{"op": "add", "text": "new fact", "confidence": 0.9}]))


def test_lambda_handler_classification_of_a_domain_wide_fault(handler, monkeypatch):
    """Companion to N16-obs32: the batch outcome the SQS trigger sees — the message is
    reported back for redelivery, not acked as poison."""
    world = World(handler)
    world.deps.opensearch = FaultyOpenSearch(
        world.os,
        {
            m: REAL_FAULTS["unavailable_503"]
            for m in ("get_doc", "put_doc", "update_doc", "knn_search", "search", "request")
        },
    )
    monkeypatch.setattr(handler, "default_deps", lambda: world.deps)
    body = {
        "kind": "memory_extract",
        "store_id": STORE,
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "actor_id": ACTOR,
        "session_id": "s1",
        "event_key": "memory/x/events/none",
        "long_term": True,
        "bucket": BUCKET,
    }
    out = handler.lambda_handler({"Records": [{"messageId": "m1", "body": json.dumps(body)}]}, None)
    assert out["batchItemFailures"] == [{"itemIdentifier": "m1"}]


@pytest.mark.parametrize("fault", ["throttle_429", "unavailable_503", "read_timeout"])
def test_lambda_handler_belt_retries_a_raw_transient_exception(handler, monkeypatch, fault):
    """N16-obs32 belt: a transport / overload exception that escapes a processor RAW is still
    a redelivery; a content error (`ValueError`) and a 4xx the content caused stay poison."""
    monkeypatch.setattr(handler, "default_deps", lambda: None)
    record = {"messageId": "m1", "body": json.dumps({"kind": "memory_extract"})}

    def raising(exc):
        def process(body, deps):
            raise exc
        return process

    monkeypatch.setattr(handler, "process_message", raising(REAL_FAULTS[fault]))
    assert handler.lambda_handler({"Records": [record]}, None) == {
        "batchItemFailures": [{"itemIdentifier": "m1"}]}
    for poison in (ValueError("unknown kind"), core.OpenSearchError(400, "parsing_exception")):
        monkeypatch.setattr(handler, "process_message", raising(poison))
        assert handler.lambda_handler({"Records": [record]}, None) == {"batchItemFailures": []}


def test_a_failed_terminal_bookkeeping_write_keeps_the_extraction(handler, caplog):
    """N16-obs32: the records are in the index when the COMPLETED write fails — the outcome is
    COMPLETED (acked), the stale IN_PROGRESS row is logged, no redelivery re-runs the model."""
    world = World(handler)
    world.emb.assign("new fact", rand_unit("new"))

    class JobsWriteFails:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            attr = getattr(self.inner, name)
            if name != "put_doc":
                return attr

            def put_doc(index, doc_id, doc, refresh=True):
                if index == JOBS and doc.get("status") == "COMPLETED":
                    raise REAL_FAULTS["unavailable_503"]
                return attr(index, doc_id, doc, refresh=refresh)

            return put_doc

    world.deps.opensearch = JobsWriteFails(world.os)
    with caplog.at_level(logging.WARNING, logger="launchpad.memory_extractor"):
        result = world.run_event(json.dumps([{"op": "add", "text": "new fact", "confidence": 0.9}]))
    assert result["status"] == "COMPLETED" and result["counts"]["facts"] == 1
    assert len(world.docs) == 1
    jobs = [j for j in world.os.indices[JOBS].values() if j.get("kind") == "memory_extract"]
    assert [j["status"] for j in jobs] == ["IN_PROGRESS"]
    assert any("status write 'COMPLETED' failed (OpenSearchError)" in r.message
               for r in caplog.records)
    assert BODY_MARKER not in caplog.text
