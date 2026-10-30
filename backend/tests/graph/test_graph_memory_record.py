# ruff: noqa: F811  — pytest fixtures are imported by name from the r16-d merge properties
"""E06 — the long-term memory record lifecycle graph walked through the extractor and the
console routes (round 16, r16-o).

Graph: ``tests/graph/graphs/memory_record.yaml``. Hermetic: the ``World`` of
``tests/property/test_memory_merge_properties.py`` (r16-d) — the extractor Lambda's merge over
one fake substrate that the hub backend (registered for the routes) shares, with every text's
vector assigned by the test so similarities are chosen, not guessed. The administrator reads
and deletes through ``/api/memory/records``; bob is a member who created no agent.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from app.services import memory_backend
from tests.conftest import set_default_resources
from tests.graph import neighbourhood
from tests.graph.executor import GRAPHS, World, assert_tables_explicit, params_for
from tests.graph.walks import run_walk, walk_seeds
from tests.property.ledger_invariants import record_is_active, scan_memory_index
from tests.property.test_memory_merge_properties import (  # noqa: F401 — fixtures by name
    ACTOR,
    CAP_MIN,
    FACTS,
    SUBSTRATE,
    _schema,
    far_unit,
    handler,
    rand_unit,
    with_cosine,
)
from tests.property.test_memory_merge_properties import (
    World as MemoryWorld,
)
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
TEXT = "likes tea"
OTHER_ACTOR = "agent2__other"


class MemoryRecordWorld(World):
    graph = "memory_record"
    UNDRIVEN: dict[str, str] = {}
    # N16-obs114 (api_delete_superseded) was pinned here until cn/r16-q made `delete_record`
    # unlink the history neighbours; the edge asserts the successor's `supersedes` now.
    XFAIL: dict[str, str] = {}

    def __init__(self, clients, handler) -> None:
        super().__init__(clients)
        self.w = MemoryWorld(handler)
        memory_backend.set_backend(self.w.hub())
        set_default_resources(SUBSTRATE)
        self.base = rand_unit("tea")
        self.rid: str | None = None  # the record under test
        self.newer: str | None = None  # the record that superseded / consolidated it
        self.events = 0

    # -- helpers -------------------------------------------------------------------
    def extract(self, ops: list[dict], **kw) -> dict:
        self.events += 1
        return self.w.run_event(json.dumps(ops), **kw)

    def doc(self, rid: str | None = None) -> dict | None:
        return self.w.docs.get(rid or self.rid)

    def listed(self, *, include_superseded: bool = False) -> list[dict]:
        params = {"actor_id": ACTOR}
        if include_superseded:
            params["include_superseded"] = "true"
        res = self.admin.get("/api/memory/records", params=params)
        assert res.status_code == 200, res.text
        return res.json()["items"]

    def extra_counts(self) -> dict[str, int]:
        import hashlib
        import json

        docs = self.w.docs
        payload = json.dumps({rid: {k: v for k, v in d.items() if k != "embedding"}
                              for rid, d in docs.items()}, sort_keys=True, default=str)
        return {"memory_docs": len(docs),
                "memory_digest": hashlib.sha256(payload.encode()).hexdigest()}  # type: ignore[dict-item]

    def check_invariants(self, edge) -> None:
        super().check_invariants(edge)
        violations = scan_memory_index(self.w.docs)
        assert violations == [], (edge.id, violations)

    def delete(self, client, rid: str, actor: str = ACTOR):
        return client.delete(f"/api/memory/records/{rid}", params={"actor_id": actor})

    # -- edges: the extractor's operations ------------------------------------------
    def edge_add(self, edge) -> None:
        self.w.emb.assign(TEXT, self.base)
        before = set(self.w.docs)
        result = self.extract([{"op": "add", "text": TEXT, "confidence": 0.9}])
        new = set(self.w.docs) - before
        assert len(new) == 1, (result["counts"], new)
        self.rid = new.pop()
        doc = self.doc()
        assert doc["text"] == TEXT and record_is_active(doc) and doc["last_op"] == "add"
        assert [r["record_id"] for r in self.listed()] == [self.rid]

    def current_vector(self) -> list[float]:
        return list(self.doc()["embedding"])

    def edge_add_duplicate(self, edge) -> None:
        dup = f"enjoys a cup of tea {self.events}"
        self.w.emb.assign(dup, with_cosine(self.current_vector(), 0.95, self.events))
        result = self.extract([{"op": "add", "text": dup, "confidence": 0.9}])
        assert result["counts"].get("duplicates", 0) >= 1, result["counts"]
        assert record_is_active(self.doc())

    def edge_update(self, edge) -> None:
        doc = self.doc()
        created, updated = doc["created_at"], doc.get("updated_at")
        text = f"likes strong tea {self.events}"
        self.w.emb.assign(text, with_cosine(self.current_vector(), 0.85, 100 + self.events))
        self.w.now = self.w.now + timedelta(minutes=1)
        self.extract([{"op": "update", "target_id": "c1", "text": text, "confidence": 0.9}])
        doc = self.doc()
        assert doc is not None and doc["text"] == text, doc  # same id, text rewritten in place
        assert doc["created_at"] == created and doc["updated_at"] != updated
        assert record_is_active(doc)

    def edge_delete_low_confidence(self, edge) -> None:
        result = self.extract([{"op": "delete", "target_id": "c1", "confidence": 0.5}])
        assert result["counts"].get("noops", 0) >= 1, result["counts"]
        assert record_is_active(self.doc())

    def edge_supersede(self, edge) -> None:
        text = "switched to coffee"
        self.w.emb.assign(text, with_cosine(self.current_vector(), 0.7, 200 + self.events))
        before = set(self.w.docs)
        self.extract([{"op": "supersede", "target_id": "c1", "text": text, "confidence": 0.9}])
        new = set(self.w.docs) - before
        assert len(new) == 1
        self.newer = new.pop()
        old, fresh = self.doc(), self.doc(self.newer)
        assert old["superseded_by"] == self.newer and fresh["supersedes"] == [self.rid]
        assert not record_is_active(old) and record_is_active(fresh)
        assert self.rid not in {r["record_id"] for r in self.listed()}

    def edge_soft_delete(self, edge) -> None:
        self.extract([{"op": "delete", "target_id": "c1", "confidence": 0.9}])
        doc = self.doc()
        assert doc is not None and doc.get("deleted_at"), doc  # a tombstone, never a hard delete
        assert not record_is_active(doc)
        assert self.rid not in {r["record_id"] for r in self.listed()}

    def edge_consolidate(self, edge) -> None:
        # the namespace goes over the cap: ours is the OLDEST active record (alias r1), the
        # seeded ones are newer, so the pass offers ours to the model first
        vectors = [self.base]
        for i in range(CAP_MIN):
            v = far_unit(vectors, 7000 + i, ceiling=0.5)
            vectors.append(v)
            self.w.seed(None, f"filler fact {i}", v, created=self.w.now + timedelta(days=i + 1))
        merged = "likes tea and the first filler"
        self.w.emb.assign(merged, far_unit(vectors, 8000, ceiling=0.5))
        before = set(self.w.docs)
        self.w.now = self.w.now + timedelta(days=CAP_MIN + 2)
        result = self.extract([], extra_outputs=(json.dumps(
            [{"text": merged, "merges": ["r1", "r2"], "confidence": 0.9}]),),
            merge={"max_facts_per_actor": CAP_MIN})
        assert result["counts"].get("consolidated") == 2, result["counts"]
        new = set(self.w.docs) - before
        assert len(new) == 1
        self.newer = new.pop()
        fresh, old = self.doc(self.newer), self.doc()
        assert fresh["last_op"] == "consolidate" and self.rid in fresh["supersedes"]
        assert old["superseded_by"] == self.newer and not record_is_active(old)

    # -- edges: the console reads ------------------------------------------------------
    def edge_list_hides_inactive(self, edge) -> None:
        shown = {r["record_id"] for r in self.listed()}
        assert self.rid not in shown and self.newer in shown
        every = {r["record_id"]: r for r in self.listed(include_superseded=True)}
        assert self.rid in every and every[self.rid]["active"] is False
        assert every[self.newer]["active"] is True

    # -- edges: the console delete -------------------------------------------------------
    def _api_delete(self, edge) -> None:
        body = self.expect(self.delete(self.admin, self.rid), edge)
        assert body["deleted"] is True and body["record_id"] == self.rid
        assert self.doc() is None

    edge_api_delete_active = _api_delete
    edge_api_delete_soft_deleted = _api_delete

    def edge_api_delete_superseded(self, edge) -> None:
        """MB_DELETE_UNLINK (N16-obs114 fixed in cn/r16-q): the retired record goes and the
        record that superseded / consolidated it no longer names it; the scan after the
        edge (`check_invariants`) is the dangling-link oracle."""
        self._api_delete(edge)
        newer = self.doc(self.newer)
        assert newer is not None and self.rid not in (newer.get("supersedes") or []), newer
        assert record_is_active(newer)
        shown = {r["record_id"]: r for r in self.listed(include_superseded=True)}
        assert self.rid not in shown and shown[self.newer]["supersedes"] == [
            m for m in shown[self.newer]["supersedes"] if m != self.rid]

    def edge_api_delete_again(self, edge) -> None:
        self.expect(self.delete(self.admin, self.rid), edge)

    def edge_api_delete_wrong_actor(self, edge) -> None:
        self.expect(self.delete(self.admin, self.rid, actor=OTHER_ACTOR), edge)
        assert self.doc() is not None

    def edge_member_delete_other_agents_record(self, edge) -> None:
        body = self.expect(self.delete(self.bob, self.rid), edge)
        assert body["detail"]["actor_id"] == ACTOR
        assert self.doc() is not None


@pytest.fixture
def make_world(admin, ada, bob, handler):
    """A fresh world (a fresh substrate) per call — the walk shrinker replays on fresh worlds."""
    _ada_id, ada_client = ada
    _bob_id, bob_client = bob
    return lambda: MemoryRecordWorld(
        {"admin": admin, "ada": ada_client, "bob": bob_client}, handler)


@pytest.fixture
def world(make_world) -> MemoryRecordWorld:
    return make_world()


def test_every_edge_is_driven_or_declared():
    assert_tables_explicit(MemoryRecordWorld)


@pytest.mark.parametrize("path", params_for(MemoryRecordWorld))
def test_memory_record_path(world, path):
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize("path", params_for(MemoryRecordWorld, include_covered_pairs=True))
def test_soak_memory_record_every_edge_pair(world, path):
    world.run(path)


@pytest.mark.parametrize("seed", walk_seeds())
def test_memory_record_walk(make_world, seed):
    """Seeded random walk (20–60 edges) through every enabled edge of the graph — refusal
    loops included — on a fresh world; a failure is shrunk on fresh worlds and reported
    with its minimal edge sequence + repro-hash (``tests/graph/walks.py``)."""
    run_walk(make_world, MemoryRecordWorld, GRAPHS["memory_record"], seed)


@pytest.mark.parametrize("path", neighbourhood.params_for(MemoryRecordWorld, GRAPHS))
def test_memory_record_neighbourhood(world, path):
    """Loop (a): every edge pair within distance 2 of a pinned (register) edge, every 3-edge
    window through it and every prime path containing it, as its own test id
    (``tests/graph/neighbourhood.py``) — strict xfail while the pin stands."""
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize(
    "path", neighbourhood.params_for(MemoryRecordWorld, GRAPHS, neighbourhood.SOAK_KINDS))
def test_soak_memory_record_neighbourhood_pairs(world, path):
    """The distance-2 edge pairs around every pinned edge (re-runs of enumerated paths)."""
    world.run(path)
