"""Unit tests of the fault layer's generator (round 6, r17-l): the shapes catalogue is built from
the recorded fixtures, a fault walk is deterministic, places its 1–3 faults on servable steps
(transition seams first) and continues from the contract's failure node, the shrinker keeps the
faulted step, and the verify / soak parameter shapes are what testing.md §6.9 says."""

from __future__ import annotations

from botocore.exceptions import ClientError, ReadTimeoutError

from app.services import opensearch_http as core
from app.services.agentcore.runtime import SdkStreamError
from tests.graph import faults
from tests.graph.executor import GRAPHS
from tests.graph.fault_seams import WORLDS
from tests.graph.paths import Edge


def test_every_shape_names_its_source_and_builds_its_exception():
    assert set(faults.FAMILIES) == {s.family for s in faults.SHAPES.values()} | {"absent",
                                                                               "interrupted"}
    for shape in faults.SHAPES.values():
        assert shape.source and shape.family in faults.FAMILIES, shape.name
        if shape.kind == "client":
            exc = shape.exception("Op")
            assert isinstance(exc, ClientError) and exc.response["Error"]["Code"] == shape.code
        elif shape.kind == "transport":
            assert isinstance(shape.exception(), ReadTimeoutError)
        elif shape.kind == "opensearch":
            exc = shape.exception()
            assert isinstance(exc, core.OpenSearchError) and exc.status == shape.status
        elif shape.kind == "frame":
            assert isinstance(shape.exception(), SdkStreamError)
            assert set(shape.frame()) == {"error", "error_type", "message"}
        elif shape.kind == "lines":
            assert shape.lines and all(isinstance(line, str) for line in shape.lines)


def test_recorded_shapes_carry_the_real_text():
    """The fixtures are the 2026-10-19 recordings, not hand-rolled stubs (docs-notes.md)."""
    nf = faults.SHAPES["rt_not_found"]
    assert nf.status == 404 and "No endpoint or agent found with qualifier 'DEFAULT'" in nf.message
    rt = faults.SHAPES["rt_read_timeout"]
    assert "/runtimes/arn%3Aaws-cn%3Abedrock-agentcore" in rt.endpoint
    assert "Read timed out" in rt.message
    ssm = faults.SHAPES["ssm_parameter_not_found"]
    assert ssm.status == 400 and ssm.message == ""  # the REAL shape: HTTP 400, empty message
    conflict = faults.SHAPES["rt_conflict_modifying"]
    assert conflict.code == "ConflictException" and "Current status: CREATING" in conflict.message
    iam = faults.SHAPES["os_iam_403"]
    assert iam.status == 403 and "is not authorized to perform: es:ESHttpGet" in iam.body
    partial = faults.SHAPES["rt_partial_frame_only"]
    assert len(partial.lines) == 1 and partial.lines[0].startswith('data: {"error": ')
    assert not partial.lines[0].rstrip().endswith("}")  # cut mid-frame, as recorded


def test_transient_predicate_agrees_with_the_shared_classifier():
    assert faults.SHAPES["runtime_bedrock_5xx_client"].transient
    assert faults.SHAPES["runtime_throttle_frame"].transient
    assert faults.SHAPES["rt_read_timeout"].transient
    assert not faults.SHAPES["rt_not_found"].transient
    assert not faults.SHAPES["rt_access_denied"].transient


def test_fault_walks_are_deterministic_and_fault_servable_transition_steps():
    for name, cls in WORLDS.items():
        graph = GRAPHS[name]
        for family in faults.FAMILIES:
            for seed in range(6):
                w1 = faults.random_fault_walk(graph, seed, family, cls)
                w2 = faults.random_fault_walk(graph, seed, family, cls)
                assert w1 == w2
                assert 0 <= len(w1.faults) <= faults.MAX_FAULTS
                node = graph.initial
                for step in w1.steps:
                    assert step.edge.src == node, (name, family, seed, step.token)
                    if step.shape is not None:
                        assert step.shape in cls.fault_shapes(step.edge, family)
                        assert step.dst == cls.fault_dst(step.edge, step.shape)
                    else:
                        assert step.dst == step.edge.dst
                    node = step.dst
                # transition seams first: a faulted REFUSAL is always a refusal slot (drawn at a
                # step where no transition seam was enabled); a transition slot is served only
                # at a transition seam — it carries forward, never degrades into a refusal
                assert len(w1.slot_kinds) == len(w1.faults)
                for step, kind in zip(w1.faults, w1.slot_kinds, strict=True):
                    if step.edge.is_refusal:
                        assert kind == "refusal", (name, family, seed, step.token)
                    else:
                        assert kind in ("transition", "refusal")


def test_verify_and_soak_parameter_shapes(monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_SOAK", raising=False)
    monkeypatch.delenv("LAUNCHPAD_LOOP_ROUND", raising=False)
    verify = faults.verify_params()
    assert [seed for _f, seed in verify] == list(range(faults.VERIFY_SEEDS))
    assert len({fam for fam, _s in verify}) == faults.VERIFY_SEEDS  # four families per gate
    monkeypatch.setenv("LAUNCHPAD_SOAK", "1")
    monkeypatch.setenv("LAUNCHPAD_LOOP_ROUND", "6")
    soak = faults.soak_params()
    assert len(soak) == len(faults.FAMILIES) * faults.SOAK_SEEDS_PER_FAMILY
    assert {seed for _f, seed in soak} == set(range(5 * 20, 6 * 20))  # round 6's fresh window


def test_the_seeded_fault_walks_are_off_the_gate_unless_asked_for(monkeypatch):
    """N17-obs287 (design review 10.5): `make verify` collects the seeded fault walks but skips
    them; `LAUNCHPAD_FAULT_WALKS=1` (make graph-faults / graph-coverage) runs the fixed verify
    shape, `LAUNCHPAD_SOAK=1` (make graph-soak) the full family window."""
    monkeypatch.delenv("LAUNCHPAD_SOAK", raising=False)
    monkeypatch.delenv("LAUNCHPAD_FAULT_WALKS", raising=False)
    gate = faults.params()
    assert len(gate) == faults.VERIFY_SEEDS
    assert all(faults.OFF_GATE in p.marks for p in gate)
    monkeypatch.setenv("LAUNCHPAD_FAULT_WALKS", "1")
    asked = faults.params()
    assert [p.values for p in asked] == [p.values for p in gate]  # same fixed seeds
    assert not any(p.marks for p in asked)
    monkeypatch.delenv("LAUNCHPAD_FAULT_WALKS")
    monkeypatch.setenv("LAUNCHPAD_SOAK", "1")
    assert not any(p.marks for p in faults.params())


class _World:
    """Fails at the faulted step once a given edge was walked before it."""

    XFAIL: dict[str, str] = {}

    def __init__(self, trigger: str) -> None:
        self.trigger = trigger
        self.seen = 0

    def step(self, edge: Edge) -> None:
        if edge.id == self.trigger:
            self.seen += 1

    def fault_step(self, edge: Edge, shape, *, expected_dst: str) -> None:
        if self.seen:
            raise faults.HalfWrite(f"{edge.id}@{shape.label} after {self.trigger}")

    def close(self) -> None:
        pass


def test_shrink_keeps_the_faulted_step_and_its_trigger():
    graph = GRAPHS["agent"]
    cls = WORLDS["agent"]
    walk = next(
        w for seed in range(40)
        for w in [faults.random_fault_walk(graph, seed, "5xx", cls)]
        if w.faults and any(s.edge.id == "redeploy" for s in walk_prefix(w))
    )
    failure = faults.run_steps(_World("redeploy"), walk.steps)
    assert failure is not None and isinstance(failure.exc, faults.HalfWrite)
    minimal, final, _replays = faults.shrink(lambda: _World("redeploy"), walk.steps, failure)
    assert minimal[-1].shape is not None and final.same_as(failure)
    assert any(s.edge.id == "redeploy" for s in minimal[:-1])
    assert len(minimal) < len(walk.steps)


def walk_prefix(walk: faults.FaultWalk) -> tuple[faults.Step, ...]:
    first = next(i for i, s in enumerate(walk.steps) if s.shape is not None)
    return walk.steps[:first]


def test_known_symptoms_are_anchored_on_the_failing_edge(monkeypatch):
    """rev-r17 nit B on round 6: a registered symptom is (edge prefix, message substring) — the
    same oracle text produced by ANOTHER edge is a new finding, never a silent xfail. The table
    shrinks as fixers flip rows, so the test installs its own entries instead of relying on a
    live one (an EMPTY prefix is the one route-independent shape, by design)."""
    monkeypatch.setitem(faults.KNOWN_SYMPTOMS, ("cold_redeploy_fails", "('active', 'failed')"),
                        "N17-test-anchored")
    monkeypatch.setitem(faults.KNOWN_SYMPTOMS, ("", "escaped the error handlers as Nowhere"),
                        "N17-test-everywhere")
    exc = AssertionError("AssertionError: ('active', 'failed')")
    assert faults.known_symptom(exc, "cold_redeploy_fails") == "N17-test-anchored"
    assert faults.known_symptom(exc, "cold_redeploy_fails@rt_conflict_modifying/deploy") == \
        "N17-test-anchored"
    assert faults.known_symptom(exc, "deploy_fails") is None
    assert faults.known_symptom(exc, "deploy_fails@rt_conflict_modifying/deploy") is None
    everywhere = faults.EnvelopeViolation("x: escaped the error handlers as Nowhere (…)")
    assert faults.known_symptom(everywhere, "stop_open@rt_read_timeout") == "N17-test-everywhere"
    assert faults.known_symptom(everywhere, "anything") == "N17-test-everywhere"
    assert faults.known_symptom(AssertionError("unrelated"), "cold_redeploy_fails") is None
    # a FIXED finding's text pins nothing once its row is gone from the table
    assert all(key[1] != "the failed extraction left a half-written pair"
               for key in faults.KNOWN_SYMPTOMS), "N17-obs90 is fixed (cn/r17-n)"
