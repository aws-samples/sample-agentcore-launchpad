"""N16-obs114 (round 16, fix cn/r16-q): the console record delete
(`SelfHostedMemoryBackend.delete_record`) unlinks its merge-v2 history neighbours.

Property, over every chain shape Hypothesis can draw (a linear supersede chain with
optional consolidate fan-ins whose members are NEAR-DUPLICATES — cosine ≥ 0.92, as a
consolidation group is by construction — and a random record deleted): after the delete the
index scan (`ledger_invariants.scan_memory_index`) is clean — no `supersedes` /
`superseded_by` names a gone document and no two ACTIVE near-duplicates — and no document
other than the deleted record's direct neighbours changed. Head case (rev-r16 ruling on
cn/r16-q): exactly one predecessor, the newest, comes back active; the others are re-pointed
at it.
Contract: memory-backend.md "Merge strategy (v2)" › history and the record delete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.services import opensearch_http as core
from app.services.memory_selfhosted import SelfHostedMemoryBackend
from tests.conftest import ws_ctx
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS
from tests.property.ledger_invariants import record_is_active, scan_memory_index
from tests.property.test_memory_merge_properties import far_unit, with_cosine
from tests.test_memory_selfhosted import SUBSTRATE

INDEX = core.memory_index("")
STORE = SUBSTRATE["memory_id"]
ACTOR = "agent0001-aaaa-bbbb-cccc-000000000001__river"
T0 = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
HYP = settings(max_examples=120, deadline=None,
               suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


def _backend() -> tuple[SelfHostedMemoryBackend, FakeOpenSearch]:
    os_ = FakeOpenSearch()
    os_.ensure_index(INDEX, core.memory_index_body())
    os_.ensure_index(core.jobs_index(""), core.jobs_index_body())
    impl = SelfHostedMemoryBackend(s3=FakeS3(), opensearch=os_, sqs=FakeSQS(),
                                   embedder=FakeEmbedder(), clock=lambda: T0)
    return impl, os_


def _doc(rid: str, slot: int, vector: list[float], **fields: Any) -> dict[str, Any]:
    """`slot` orders the records in time (distinct `updated_at`), so "the newest predecessor"
    is well defined; `vector` is the embedding the dedupe scan compares."""
    stamp = (T0 + timedelta(minutes=slot)).isoformat()
    return {
        "store_id": STORE, "workspace_id": "default", "actor_id": ACTOR,
        "namespace": f"/facts/{ACTOR}", "strategy": "semantic", "session_id": None,
        "text": f"fact {rid}", "embedding": list(vector),
        "created_at": stamp, "updated_at": stamp, "confidence": 0.9, "last_op": "add",
        "source_event_ids": [], "metadata": {}, "supersedes": [], **fields,
    }


# a chain: node i (0 = oldest) is superseded by node i+1; each link may fan in extra
# "member" records (a consolidate), which the next node supersedes alongside the chain node
chain_st = st.lists(st.integers(min_value=0, max_value=2), min_size=1, max_size=5)


def _build(os_: FakeOpenSearch, fan_ins: list[int]) -> list[str]:
    """Returns every record id; the chain is c0 → c1 → … → cN (N = len(fan_ins)). Chain
    nodes are far apart (< 0.5 cosine); a consolidate group's members sit at cosine ≥ 0.95
    from their chain node — near-duplicates, as the consolidation pass groups them."""
    ids: list[str] = []
    docs: dict[str, dict[str, Any]] = {}
    chain = [f"c{i}" for i in range(len(fan_ins) + 1)]
    vectors: list[list[float]] = []
    slot = 0
    for rid in chain:
        v = far_unit(vectors, 500 + slot, ceiling=0.5)
        vectors.append(v)
        docs[rid] = _doc(rid, slot, v)
        slot += 1
    for i, extra in enumerate(fan_ins):
        members = [chain[i]]
        for k in range(extra):
            mid = f"m{i}_{k}"
            docs[mid] = _doc(mid, slot, with_cosine(docs[chain[i]]["embedding"], 0.96, 900 + slot))
            slot += 1
            members.append(mid)
        nxt = chain[i + 1]
        docs[nxt]["supersedes"] = members
        docs[nxt]["last_op"] = "consolidate" if extra else "supersede"
        for m in members:
            docs[m]["superseded_by"] = nxt
            docs[m]["superseded_at"] = T0.isoformat()
    for rid, d in docs.items():
        os_.put_doc(INDEX, rid, d)
        ids.append(rid)
    assert scan_memory_index({r: dict(d) for r, d in os_.indices[INDEX].items()}) == []
    return ids


def _newest(docs: dict[str, dict[str, Any]], ids: list[str]) -> str:
    return max(ids, key=lambda r: (core.record_timestamp(docs[r]), r))


@HYP
@given(fan_ins=chain_st, pick=st.data())
def test_any_delete_inside_a_chain_leaves_no_dangling_link(fan_ins, pick):
    impl, os_ = _backend()
    ids = _build(os_, fan_ins)
    victim = pick.draw(st.sampled_from(ids))
    before = {r: dict(d) for r, d in os_.indices[INDEX].items()}
    doc = before[victim]
    successor = doc.get("superseded_by")
    predecessors = list(doc.get("supersedes") or [])

    out = impl.delete_record(ws_ctx(SUBSTRATE, region="cn-northwest-1", id="default"),
                             victim, ACTOR)

    assert out["deleted"] is True
    after = {r: dict(d) for r, d in os_.indices[INDEX].items()}
    assert victim not in after
    assert scan_memory_index(after) == [], after
    # nobody names the gone record any more
    for rid, d in after.items():
        assert d.get("superseded_by") != victim, rid
        assert victim not in (d.get("supersedes") or []), rid
    # only the direct neighbours changed, and only their two link fields
    touched = {successor, *predecessors} - {None}
    for rid, d in after.items():
        if rid not in touched:
            assert d == before[rid], rid
        else:
            for key in d:
                if key not in ("supersedes", "superseded_by", "superseded_at"):
                    assert d[key] == before[rid][key], (rid, key)
    # the successor keeps every other member and inherits the victim's predecessors
    if successor:
        assert set(after[successor]["supersedes"]) == (
            set(before[successor]["supersedes"]) - {victim}) | set(predecessors)
        for m in predecessors:  # re-chained: still retired, now by the successor
            assert after[m]["superseded_by"] == successor and not record_is_active(after[m])
    elif predecessors:
        # head case: exactly the newest predecessor comes back active; the other members
        # (near-duplicates of it) are re-pointed at it, so no two ACTIVE near-duplicates
        newest = _newest(before, predecessors)
        assert after[newest]["superseded_by"] is None and after[newest]["superseded_at"] is None
        assert record_is_active(after[newest])
        others = [m for m in predecessors if m != newest]
        assert set(after[newest]["supersedes"]) >= set(others)
        for m in others:
            assert after[m]["superseded_by"] == newest and not record_is_active(after[m])
        assert sum(record_is_active(after[m]) for m in predecessors) == 1
    # touched neighbours changed only their link fields (checked above); the newest
    # predecessor may additionally have gained `supersedes` — already covered by the
    # key-by-key loop, which allows the three link fields only
