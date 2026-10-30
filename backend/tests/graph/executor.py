"""The API executor's shared machinery (round 16, channel A, r16-o).

A **world** is one entity's hermetic stage: the auth-enabled console with an administrator
and two members (``tests/test_visibility.py``), the AWS seams stubbed, and one method per
graph edge named ``edge_<id>`` that performs the action through the real routes and asserts
the status / code the contract names (``Edge.expect``). ``run_path`` walks a test path
edge by edge and, after EVERY edge:

* a ``kind: refusal`` edge must have written nothing — every ledger table the world watches
  carries the same per-table digest (sha256 over the sorted rows' full column tuples, so an
  in-place UPDATE counts as a write) and its own extra stores are unchanged;
* the shared ledger scan (``tests/property/ledger_invariants.scan_ledger``) must report no
  violation naming a row this world created (restricted on purpose: another test's detached
  turn driver may land rows in the worker's ledger — r16-e lesson);
* the walked edge is recorded for the coverage report.

A path that needs an edge no world can drive hermetically is skipped with the reason from
the world's ``UNDRIVEN`` table (so the gap is explicit); a path through a registered finding
is pinned ``xfail(strict=True)`` from its ``XFAIL`` table.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.evaluation.models import EvalDataset, EvalEvaluator, EvalResult, EvalRun, OnlineEvalConfig
from app.models.assistant import AssistantConversation, AssistantMessage, AssistantProposal
from app.models.ledger import (
    Agent,
    ChatMessage,
    ChatSession,
    ChatTurnEvent,
    Deployment,
    Job,
    KnowledgeBase,
    ManagedMemory,
    SpecSnapshot,
)
from app.optimization.models import ExperimentAssignment, RuntimeCanary
from tests.graph import coverage
from tests.graph.paths import Edge, Graph, TestPath, load_all, walk_paths
from tests.property.ledger_invariants import scan_ledger

GRAPHS: dict[str, Graph] = load_all()

COUNTED = (
    Agent, Deployment, Job, SpecSnapshot, ChatSession, ChatMessage, ChatTurnEvent,
    EvalRun, EvalResult, EvalDataset, EvalEvaluator, OnlineEvalConfig,
    RuntimeCanary, ExperimentAssignment,
    AssistantConversation, AssistantProposal, AssistantMessage,
    KnowledgeBase, ManagedMemory,
)


def ledger_counts() -> dict[str, int]:
    db = SessionLocal()
    try:
        return {
            model.__tablename__: db.scalar(select(func.count()).select_from(model)) or 0
            for model in COUNTED
        }
    finally:
        db.close()


def _row_tuple(row: Any) -> tuple:
    return tuple(
        repr(getattr(row, column.key)) for column in row.__table__.columns
    )


def ledger_digest() -> dict[str, str]:
    """One sha256 per counted table over its rows' full column tuples (sorted), so a refused
    call that merely UPDATED a row (stamped `ended_at`, flipped a `status`, bumped `turns`)
    changes the digest exactly like an INSERT or DELETE would (rev-r16 on 3d109fd3)."""
    db = SessionLocal()
    try:
        out: dict[str, str] = {}
        for model in COUNTED:
            rows = sorted(_row_tuple(r) for r in db.scalars(select(model)).all())
            out[model.__tablename__] = hashlib.sha256(repr(rows).encode()).hexdigest()
        return out
    finally:
        db.close()


class World:
    """Base class: subclasses set ``graph`` and implement ``edge_<id>`` methods."""

    graph: str = ""
    #: edges no hermetic driver exists for → the reason (a path through one is SKIPPED)
    UNDRIVEN: dict[str, str] = {}
    #: edges whose contract outcome is a registered finding → the xfail reason (strict)
    XFAIL: dict[str, str] = {}
    #: register id → edges whose pin was FIXED: they stay in the neighbourhood set
    #: (``tests/graph/neighbourhood.py``) without the xfail mark, so the re-exploration around
    #: a fixed finding keeps running after the fix (rev-r17 on r17-b)
    FIXED: dict[str, list[str]] = {}

    def __init__(self, clients: dict[str, TestClient]) -> None:
        self.clients = clients
        self.ids: set[str] = set()  # ledger ids this world created (scan filter)
        self.log: list[str] = []

    # -- helpers for the edge methods -------------------------------------------------
    @property
    def admin(self) -> TestClient:
        return self.clients["admin"]

    @property
    def ada(self) -> TestClient:
        return self.clients["ada"]

    @property
    def bob(self) -> TestClient:
        return self.clients["bob"]

    def note(self, text: str) -> None:
        self.log.append(text)

    def expect(self, res: Any, edge: Edge, **overrides: Any) -> dict[str, Any] | None:
        """Assert the status (and the error code) the edge names; return the JSON body."""
        want = {**edge.expect, **overrides}
        status = want.get("status")
        if status is not None:
            assert res.status_code == status, (edge.id, res.status_code, res.text[:400])
        body = None
        try:
            body = res.json()
        except ValueError:
            pass
        if want.get("code") is not None and isinstance(want["code"], str) \
                and " " not in want["code"]:
            assert body is not None and body.get("code") == want["code"], (edge.id, body)
        return body

    def extra_counts(self) -> dict[str, int]:
        """Stores outside the SQLite ledger a refusal must leave untouched (e.g. the memory
        index)."""
        return {}

    def counts(self) -> dict[str, int]:
        return {**ledger_counts(), **self.extra_counts()}

    def digest(self) -> dict[str, Any]:
        """What a refusal must leave untouched: the per-table row digests plus the world's own
        extra stores (counts — the memory index is compared by document set below)."""
        return {**ledger_digest(), **self.extra_counts()}

    def check_invariants(self, edge: Edge) -> None:
        violations = scan_ledger(SessionLocal)
        own = [v for v in violations if any(i and i in v for i in self.ids)]
        assert own == [], (edge.id, own)

    def before(self, edge: Edge) -> None:
        """Stage what an edge needs that is NOT the edge's own effect (a second completed run
        for a compare, …) — runs before the refusal snapshot, so staging is never mistaken
        for a write the refused call made."""

    def close(self) -> None:
        """Release what a walk may leave behind (a gated turn, a thread) — the walk runner
        calls it in a ``finally`` for every world it created (``tests/graph/walks.py``)."""

    # -- the walk ----------------------------------------------------------------------
    def step(self, edge: Edge) -> None:
        """One edge: stage, drive, and the three per-edge oracles (refusal digest, own-id
        ledger scan + the world's extra scans, coverage)."""
        handler: Callable[[Edge], None] = getattr(self, f"edge_{edge.id}")
        self.before(edge)
        before = self.digest() if edge.is_refusal else None
        handler(edge)
        if edge.is_refusal:
            after = self.digest()
            changed = sorted(k for k in after if before.get(k) != after[k])
            assert not changed, (
                f"refused edge {edge.id!r} changed ledger table(s) / store(s): {changed}")
        self.check_invariants(edge)
        coverage.record_edge(self.graph, edge.id)

    def run(self, path: TestPath) -> None:
        for edge in path.edges:
            self.step(edge)
        coverage.record_path(self.graph, path.id)


def driven_edges(world_cls: type[World]) -> set[str]:
    return {name[5:] for name in dir(world_cls) if name.startswith("edge_")}


REQUIREMENTS = ("prime", "edge-pair")


def params_for(world_cls: type[World], *, include_covered_pairs: bool = False,
               requirements: tuple[str, ...] = REQUIREMENTS,
               shard: tuple[int, int] = (0, 1)) -> list[Any]:
    """``pytest.param`` list for ``@pytest.mark.parametrize("path", …)`` — skip / xfail marks
    applied from the world's tables; the id is ``<graph>/P01`` plus the edge labels.
    ``requirements`` selects prime paths and / or edge pairs and ``shard = (i, n)`` keeps every
    n-th path starting at i, so a large graph spreads its paths over several files to stay
    under the per-file budget (the canary graph: 233 prime paths over two files, rev-r17)."""
    graph = GRAPHS[world_cls.graph]
    driven = driven_edges(world_cls)
    out = []
    i, n = shard
    for tp in walk_paths(graph, include_covered_pairs=include_covered_pairs):
        if tp.requirement not in requirements or tp.index % n != i:
            continue
        marks: list[Any] = []
        missing = [e.id for e in tp.edges if e.id not in driven]
        if missing:
            reasons = "; ".join(
                f"{m}: {world_cls.UNDRIVEN.get(m, 'no hermetic driver declared')}"
                for m in missing
            )
            marks.append(pytest.mark.skip(reason=f"undriven edge(s) — {reasons}"))
        else:
            pinned = [e.id for e in tp.edges if e.id in world_cls.XFAIL]
            if pinned:
                marks.append(pytest.mark.xfail(
                    strict=True, reason="; ".join(world_cls.XFAIL[p] for p in pinned)))
        out.append(pytest.param(tp, id=f"{tp.id}[{tp.label}]", marks=marks))
    return out


def assert_tables_explicit(world_cls: type[World]) -> None:
    """Every edge of the graph is driven, or named in UNDRIVEN with a reason; every XFAIL /
    UNDRIVEN key names a real edge."""
    graph = GRAPHS[world_cls.graph]
    edge_ids = {e.id for e in graph.edges}
    driven = driven_edges(world_cls)
    silent = sorted(edge_ids - driven - set(world_cls.UNDRIVEN))
    assert silent == [], (
        f"{graph.name}: edges with neither a driver nor an UNDRIVEN reason: {silent}")
    assert set(world_cls.UNDRIVEN) <= edge_ids, set(world_cls.UNDRIVEN) - edge_ids
    assert set(world_cls.XFAIL) <= edge_ids, set(world_cls.XFAIL) - edge_ids
    assert not (set(world_cls.UNDRIVEN) & driven), set(world_cls.UNDRIVEN) & driven


def ids_of(rows: Iterable[Any]) -> set[str]:
    return {r.id for r in rows}
