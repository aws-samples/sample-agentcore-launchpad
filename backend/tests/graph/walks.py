"""Seeded random walks over the lifecycle graphs (round 17, loop engine, r17-b).

Where ``paths.py`` enumerates the prime paths and edge pairs (every edge, every adjacent
pair, once), a **walk** is a long random route through the same graph: 20–60 edges, the next
edge drawn uniformly among the edges enabled at the current node — refusal self-loops
included, so a walk hammers the refusals in the middle of a history the enumerated paths
never build (create → redeploy → delete → create-again → …). A walk is a pure function of
``(graph, seed)`` (``random.Random(seed)``), so a failing seed replays byte-identically on
any host.

The executor drives a walk edge by edge with the same per-edge oracles the test paths get
(``World.step``: the contract assertion of the edge method, the per-table digest on every
refusal, the own-id ledger scan and the world's extra scans after EVERY step). A walk that
fails is **shrunk** before it is reported: the failing prefix is cut after the failing edge,
then every cycle of the prefix (a sub-sequence that leaves and re-enters the same node — a
self-loop is the shortest) is removed while the failure stays the same failing edge, on a
fresh world per attempt. The result is the minimal edge sequence that goes into the register
row (its ``repro-hash:`` is the board's convention: sha1 of the lower-cased, whitespace-
collapsed label, 8 hex). A shrunk failure whose hash is already pinned in ``KNOWN`` is a
registered finding and xfails; an unknown one fails with the full repro text.

Edges a world pins ``XFAIL`` (a registered finding) are excluded from a walk's choices — the
neighbourhood module (``neighbourhood.py``) owns their re-exploration — so the walk keeps
exploring everything else instead of stopping at the known failure.

Budget: 8 seeds per graph in ``make verify`` (``WALK_SEEDS``), 200 under ``LAUNCHPAD_SOAK=1``.
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from tests.graph import coverage
from tests.graph.paths import Edge, Graph

MIN_LEN, MAX_LEN = 20, 60
VERIFY_SEEDS = 8
SOAK_SEEDS = 200
SHRINK_BUDGET = 40  # fresh-world replays a shrink may spend

#: ``repro-hash`` of a shrunk failing sequence → the register id + reason (a known finding).
#: The dedicated strict pin lives next to the graph's walk test; the seeded walk that runs
#: into the same minimal sequence xfails here instead of re-reporting it.
KNOWN: dict[str, str] = {}

#: Every register row the walks opened — ``(register id, graph, minimal edge sequence)`` —
#: fixed or still pinned; the loop-engine table of ``graph-coverage.md`` counts them per graph
#: (``loop_report.py``) and the board's `graph_walk_findings` / stop rule read that table.
FINDINGS: tuple[tuple[str, str, str], ...] = (
    ("N17-obs10", "eval_run",
     "create>dispatch_completes>dataset_delete_after_completed>rerun_completed>"
     "compare_with_queued"),
    ("N17-obs11", "reference",
     "memory_delete_after_agent_gone>memory_delete_after_agent_gone (and the dataset / "
     "evaluator twins) — the N17-obs10 lesson applied by audit"),
)


def repro_hash(text: str) -> str:
    """The board's `repro-hash:` convention (scripts/quality_metrics.py)."""
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]


def soak() -> bool:
    return os.environ.get("LAUNCHPAD_SOAK") == "1"


def loop_round() -> int:
    """`LAUNCHPAD_LOOP_ROUND` (default 1): the soak of round r draws a FRESH seed window
    ``[(r-1)·200, r·200)`` so a later round is never a replay of an earlier one; the verify
    shape (8 seeds) stays fixed so the gate is deterministic."""
    try:
        return max(1, int(os.environ.get("LAUNCHPAD_LOOP_ROUND", "1")))
    except ValueError:
        return 1


def walk_seeds() -> list[int]:
    """The seeds a run walks: the first 8 in verify, the round's 200-seed window under soak."""
    if not soak():
        return list(range(VERIFY_SEEDS))
    r = loop_round()
    return list(range((r - 1) * SOAK_SEEDS, r * SOAK_SEEDS))


@dataclass(frozen=True)
class Walk:
    graph: str
    seed: int
    edges: tuple[Edge, ...]
    requested: int  # the drawn length; shorter only when the walk hit a dead end

    @property
    def id(self) -> str:
        return f"{self.graph}/W{self.seed:04d}"

    @property
    def label(self) -> str:
        return ">".join(e.id for e in self.edges)

    @property
    def nodes(self) -> list[str]:
        return [self.edges[0].src, *(e.dst for e in self.edges)] if self.edges else []


def random_walk(graph: Graph, seed: int, *, exclude: frozenset[str] = frozenset(),
                min_len: int = MIN_LEN, max_len: int = MAX_LEN) -> Walk:
    """Deterministic in ``(graph, seed, exclude)``: edges are sorted by id before the draw."""
    rng = random.Random(seed)
    length = rng.randint(min_len, max_len)
    node = graph.initial
    out: list[Edge] = []
    for _ in range(length):
        choices = sorted((e for e in graph.out_edges(node) if e.id not in exclude),
                         key=lambda e: e.id)
        if not choices:
            break  # a dead end (a node with no enabled edge): the walk ends early
        edge = choices[rng.randrange(len(choices))]
        out.append(edge)
        node = edge.dst
    return Walk(graph.name, seed, tuple(out), length)


# ---------------------------------------------------------------------------
# running + shrinking
# ---------------------------------------------------------------------------


@dataclass
class Failure:
    index: int  # the failing edge's position in the sequence
    edge_id: str
    exc: BaseException

    @property
    def head(self) -> str:
        first = str(self.exc).strip().splitlines()
        return f"{type(self.exc).__name__}: {first[0][:200] if first else ''}"

    def same_as(self, other: Failure) -> bool:
        return self.edge_id == other.edge_id and type(self.exc) is type(other.exc)


def run_sequence(world: Any, edges: tuple[Edge, ...]) -> Failure | None:
    """Drive ``edges`` on ``world`` with the per-edge oracles; the first failure, or None."""
    try:
        for i, edge in enumerate(edges):
            try:
                world.step(edge)
            except Exception as exc:  # noqa: BLE001 — every oracle failure is a finding
                return Failure(i, edge.id, exc)
        return None
    finally:
        world.close()


def _cycles(edges: tuple[Edge, ...]) -> list[tuple[int, int]]:
    """Every ``(i, j)`` with ``node_before(i) == node_before(j)``, ``i < j`` — removing
    ``edges[i:j]`` keeps the sequence connected. Longest first, then leftmost."""
    nodes = [edges[0].src, *(e.dst for e in edges)]
    out = [(i, j) for i in range(len(edges)) for j in range(i + 1, len(edges) + 1)
           if nodes[i] == nodes[j]]
    return sorted(out, key=lambda ij: (-(ij[1] - ij[0]), ij[0]))


def shrink(make_world: Callable[[], Any], edges: tuple[Edge, ...], failure: Failure,
           *, budget: int = SHRINK_BUDGET) -> tuple[tuple[Edge, ...], Failure, int]:
    """Cut after the failing edge, then remove cycles while the same edge keeps failing (same
    edge id, same exception type). Returns ``(minimal, failure, replays)``."""
    current = edges[: failure.index + 1]
    current_failure = failure
    replays = 0
    progress = True
    while progress and replays < budget:
        progress = False
        # the failing edge must stay: never remove a window that swallows the last edge
        for i, j in _cycles(current):
            if j >= len(current):
                continue
            candidate = (*current[:i], *current[j:])
            if replays >= budget:
                break
            replays += 1
            got = run_sequence(make_world(), candidate)
            if got is not None and got.same_as(current_failure) \
                    and got.index == len(candidate) - 1:
                current, current_failure, progress = candidate, got, True
                break
    return current, current_failure, replays


def finding_text(graph: str, seed: int, minimal: tuple[Edge, ...], failure: Failure,
                 replays: int, full_len: int) -> str:
    label = ">".join(e.id for e in minimal)
    tb = "".join(traceback.format_exception(failure.exc)).strip().splitlines()[-12:]
    return (
        f"walk {graph}/W{seed:04d} ({full_len} edges) failed at edge {failure.edge_id!r} "
        f"(step {failure.index + 1})\n"
        f"minimal edge sequence ({len(minimal)} edges, {replays} shrink replays):\n  {label}\n"
        f"repro-hash: {repro_hash(label)}\n"
        f"{failure.head}\n" + "\n".join("  " + line for line in tb)
    )


def run_walk(make_world: Callable[[], Any], world_cls: type, graph: Graph, seed: int) -> Walk:
    """Walk ``seed`` on a fresh world; on failure shrink on fresh worlds, then xfail (known
    pin) or fail with the register-ready text. Returns the walk (for the caller's log)."""
    walk = random_walk(graph, seed, exclude=frozenset(world_cls.XFAIL))
    assert walk.edges, (graph.name, seed, "the initial node has no enabled edge")
    failure = run_sequence(make_world(), walk.edges)
    if failure is None:
        coverage.record_path(graph.name, walk.id)
        return walk
    minimal, final, replays = shrink(make_world, walk.edges, failure)
    label = ">".join(e.id for e in minimal)
    text = finding_text(graph.name, seed, minimal, final, replays, len(walk.edges))
    known = KNOWN.get(repro_hash(label))
    if known:
        pytest.xfail(f"{known} — {label}")
    raise AssertionError(text) from final.exc
