# ruff: noqa: F811  — pytest fixtures are imported by name from the world modules
"""Fault-injected walks over the MEMORY RECORD graph (round 6, r17-l; testing.md §6.9): the
extractor's and the hub's OpenSearch calls serve the recorded shapes (a once-only fault at the
candidate read or at one of the op's writes; the hub's read / delete / unlink calls)."""

from __future__ import annotations

import pytest

from tests.graph import faults
from tests.graph._fault_axis import axis_params, run_axis_cell
from tests.graph.executor import GRAPHS
from tests.graph.fault_seams import MemoryFaults, OnceFaulty, make_client_dict
from tests.property.test_memory_merge_properties import (  # noqa: F401 — fixture by name
    JOBS,
    handler,
)
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name


@pytest.fixture
def make_world(admin, ada, bob, handler):
    clients, _ada_id = make_client_dict(admin, ada, bob)
    return lambda: MemoryFaults(clients, handler)


@pytest.mark.parametrize(("family", "seed"), faults.params())
def test_memory_record_fault_walk(make_world, family, seed):
    faults.run_fault_walk(make_world, MemoryFaults, GRAPHS["memory_record"], family, seed)


@pytest.mark.parametrize(("edge_id", "shape"), axis_params(MemoryFaults, "memory_record"))
def test_memory_record_refusal_seam_axis(make_world, edge_id, shape):
    run_axis_cell(make_world, "memory_record", edge_id, shape)


# ---------------------------------------------------------------------------
# the round-6 pins (strict, narrow: the registered symptom type only — a fix flips them)
# ---------------------------------------------------------------------------


def test_N17_obs90_supersede_half_write_when_the_second_write_fails(make_world):
    """N17-obs90 (fixed in cn/r17-n): the extractor's supersede used to write the successor
    (`put_doc`) and retire the target (`update_doc`) in two requests — a fault between the two
    left both ACTIVE. The pair is ONE `bulk` request now: the recorded 503 on it leaves the
    index exactly as it was (`_faulted_extraction`'s no-half-write oracle), the job is FAILED
    with the exception type, and the redelivery applies the op."""
    world = make_world()
    graph = GRAPHS["memory_record"]
    world.step(graph.edge("add"))
    shape = faults.with_at(faults.SHAPES["os_503_no_master"], "bulk")
    world.fault_step(graph.edge("supersede"), shape, expected_dst="superseded")


def test_N17_obs91_failed_delete_of_a_chain_head_revives_its_predecessor(make_world):
    """N17-obs91 (fixed in cn/r17-n): the console delete of a chain HEAD unlinks before it
    deletes; when `delete_doc` fails the unlink is undone (`_relink_history`) — the predecessor
    it had revived is retired again, the head stays, the member gets the mapped 503."""
    world = make_world()
    graph = GRAPHS["memory_record"]
    world.step(graph.edge("add"))
    world.step(graph.edge("supersede"))
    head, tail = world.newer, world.rid
    shape = faults.with_at(faults.SHAPES["os_503_no_master"], "delete_doc")
    world._hub_over(world._faulty(shape, "delete_doc"))
    try:
        res = world.delete(world.admin, head)
    finally:
        world._disarm()
    assert res.status_code == 503 and res.json()["code"] == "memory.backend_unavailable", res.text
    docs = world.w.docs
    assert head in docs, "the failed delete removed the head"
    assert docs[tail].get("superseded_by") == head, (
        "the failed delete of the head revived its predecessor", docs[tail])


def test_N17_obs92_memory_guard_echoes_the_raw_opensearch_body(make_world):
    """N17-obs92 (fixed in cn/r17-n): `routers/memory._guard` answers an unmapped OpenSearch
    status (403 / 400) with 502 memory.unavailable and a STATIC message — the raw body (the IAM
    sentence with the role ARN, the parsing_exception root_cause) no longer reaches the
    console (`LeakedSeamText` oracle of the refusal × seam cell)."""
    world = make_world()
    graph = GRAPHS["memory_record"]
    world.step(graph.edge("add"))
    shape = faults.with_at(faults.SHAPES["os_iam_403"], "get_doc")
    world.fault_step(graph.edge("api_delete_wrong_actor"), shape, expected_dst="active")


def test_N16_obs32_rediscovered_one_fault_on_the_job_bookkeeping_escapes_raw(make_world):
    """N16-obs32 (rediscovered by the round-6 fault walks; fixed in cn/r17-n): one recorded
    OpenSearch fault on the extractor's FIRST write — the `_job(IN_PROGRESS)` bookkeeping on
    the jobs index — is a `RetryableError` (redelivery), no longer the raw OpenSearchError /
    ReadTimeoutError `lambda_handler` acked as poison."""
    world = make_world()
    graph = GRAPHS["memory_record"]
    world.step(graph.edge("add"))
    shape = faults.SHAPES["os_read_timeout"]
    world.w.deps.opensearch = OnceFaulty(world.w.os, "put_doc", shape.exception(),
                                         world.seam_calls, index=JOBS)
    try:
        with pytest.raises(world.w.h.RetryableError):
            world.edge_update(graph.edge("update"))
    finally:
        world.w.deps.opensearch = world.w.os


def test_N17_obs101_stale_candidate_read_breaks_the_dedupe_invariant(make_world):
    """N17-obs101 (backend part fixed in cn/r17-n): the dedupe rule ('nearest active record at
    cosine ≥ 0.92 → duplicate') used to depend on the candidate read seeing the previous write;
    an eventually-consistent (empty) read let the extractor ADD the near-duplicate. The
    extractor now issues a `_refresh` right before its nearest-record re-check (`EmptyRead` is
    stale until that request): the near-duplicate is counted, nothing is written, the index
    scan stays clean. The per-actor serialisation (two writes racing) is the open infra part."""
    world = make_world()
    graph = GRAPHS["memory_record"]
    world.step(graph.edge("add"))
    world.fault_step(graph.edge("add_duplicate"),
                     faults.with_at(faults.SHAPES["os_empty_read"], "knn_search"),
                     expected_dst="active")
