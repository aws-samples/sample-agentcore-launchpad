"""Canary routing in the backend (N12 E3-1, plan §B.7 / §B.8; canary-only since N13-obs2).

The backend is the routing authority: ``experiment_assignments`` is written before a
replay job is dispatched and by the live invoke chain; ``ab_results`` is computed from
``eval_results`` × assignments with a local Welch test so the baseline
``normalize_ab_results`` / ``compute_verdict`` stay verbatim.
"""

from collections import Counter

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalResult
from app.optimization import routing
from app.optimization.models import Experiment, ExperimentAssignment, RuntimeCanary

AGENT = "agent1"
STABLE, TREAT = "stableaaa111", "treataaa111"


def _mk_canary(*, status: str = "running", setup: dict | None = None,
               abtest: dict | None = None, agent_id: str = AGENT) -> RuntimeCanary:
    artifacts: dict = {"agent_meta": {"id": agent_id, "arn": "arn:rt"}, "rounds": []}
    if setup is not None:
        artifacts["setup"] = setup
    if abtest is not None:
        artifacts["abtest"] = abtest
    db = SessionLocal()
    try:
        row = RuntimeCanary(workspace_id=DEFAULT_WORKSPACE_ID, name="CAN",
                            champion_agent_id=agent_id, champion_agent_name="agent",
                            challenger_agent_id=agent_id, challenger_agent_name="agent",
                            status=status, artifacts=artifacts)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def _mk_live_canary(*, status: str = "running", execution_status: str | None = "RUNNING",
                    agent_id: str = AGENT, control_w: int = 50, treatment_w: int = 50,
                    variants: list[dict] | None = None) -> RuntimeCanary:
    """A canary past setup with a live A/B row — the generic owner of these tests."""
    abtest = None
    if execution_status is not None:
        abtest = {
            "ab_test_id": "local_can",
            "variants": variants if variants is not None
            else routing.target_variants(STABLE, TREAT, control_w, treatment_w),
            "execution_status": execution_status,
        }
    return _mk_canary(status=status, agent_id=agent_id, abtest=abtest,
                      setup={"stable_endpoint": STABLE, "treatment_endpoint": TREAT,
                             "runtime_id": "rid", "v_current": "3", "v_candidate": "4"})


def _assignments(owner_id: str) -> dict[str, str]:
    db = SessionLocal()
    try:
        rows = db.query(ExperimentAssignment).filter(
            ExperimentAssignment.owner_id == owner_id).all()
        return {r.session_id: r.arm for r in rows}
    finally:
        db.close()


# ─── active owner ────────────────────────────────────────────────────────────
def test_no_owner_without_an_active_canary():
    db = SessionLocal()
    try:
        assert routing.active_owner(db, AGENT) is None
        _mk_live_canary(execution_status="STOPPED")  # complete / rollback stopped the split
        assert routing.active_owner(db, AGENT) is None
        _mk_live_canary(status="completed")
        assert routing.active_owner(db, AGENT) is None
    finally:
        db.close()


def test_a_legacy_experiment_row_never_routes():
    """N13-obs2: the configuration experiment is retired — a leftover ``experiments`` row,
    even one whose A/B row still says RUNNING, owns nothing and writes no assignment."""
    db = SessionLocal()
    try:
        db.add(Experiment(
            workspace_id=DEFAULT_WORKSPACE_ID, name="EXP", agent_id=AGENT, agent_name="agent",
            status="running", stage="abtest",
            artifacts={"agent_meta": {"id": AGENT, "arn": "arn:rt"},
                       "abtest": {"ab_test_id": "local_exp", "execution_status": "RUNNING",
                                  "variants": routing.target_variants("c", "t", 50, 50)}},
        ))
        db.commit()
        assert routing.active_owner(db, AGENT) is None
        assert routing.route_for(db, AGENT, "s1") is None
        assert db.query(ExperimentAssignment).count() == 0
    finally:
        db.close()


def test_paused_ab_row_routes_everything_to_control():
    """PAUSED keeps the owner (sticky arms survive) but sends new sessions to C."""
    exp = _mk_live_canary(execution_status="PAUSED")
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, AGENT)
        assert owner is not None and owner.execution_status == "PAUSED"
        arms = {routing.route_for(db, AGENT, f"s{i}").arm for i in range(20)}
    finally:
        db.close()
    assert arms == {"C"}
    assert set(_assignments(exp.id).values()) == {"C"}


def test_canary_partial_setup_routes_everything_to_the_stable_endpoint():
    """The provisioning form: the stable endpoint exists but no A/B row yet — every
    session (new or sticky) must hit ``stable<id6>`` so DEFAULT (auto-rolled to the
    untested candidate by the mint) never serves production."""
    canary = _mk_canary(setup={"stable_endpoint": "stableabc123", "runtime_id": "rid",
                               "v_current": "3"})
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, AGENT)
        assert owner is not None and owner.kind == "canary" and owner.id == canary.id
        routes = [routing.route_for(db, AGENT, f"s{i}") for i in range(10)]
    finally:
        db.close()
    assert {r.arm for r in routes} == {"C"}
    assert {r.qualifier for r in routes} == {"stableabc123"}
    assert all(r.bundle_ref is None for r in routes)


def test_canary_live_ab_row_splits_by_endpoint_qualifier():
    variants = routing.target_variants("stableabc123", "treatabc123")
    canary = _mk_canary(
        setup={"stable_endpoint": "stableabc123", "treatment_endpoint": "treatabc123",
               "runtime_id": "rid", "v_current": "3", "v_candidate": "4"},
        abtest={"ab_test_id": "local_can", "variants": variants, "execution_status": "RUNNING"},
    )
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, AGENT)
        assert owner.weights == {"C": 90, "T1": 10}
        assert owner.arm("C").qualifier == "stableabc123"
        assert owner.arm("T1").qualifier == "treatabc123"
        assigned = routing.assign_batch(owner, [f"s{i}" for i in range(400)], db=db)
    finally:
        db.close()
    counts = Counter(a["arm"] for a in assigned.values())
    assert 15 <= counts["T1"] <= 70, counts  # 10 % ± a generous band over 400 ids
    assert all(a["qualifier"] in {"stableabc123", "treatabc123"} for a in assigned.values())
    assert all(a["launchpad_meta"]["experiment"] == canary.id for a in assigned.values())
    assert all("bundle" not in a["launchpad_meta"] for a in assigned.values())


def test_canaries_of_other_states_or_agents_do_not_route():
    _mk_canary(status="completed", setup={"stable_endpoint": "stable1", "runtime_id": "r"})
    _mk_canary(status="running", setup={"stable_endpoint": "stable2", "runtime_id": "r"},
               agent_id="other-agent")
    _mk_canary(status="running", setup=None)  # nothing provisioned yet
    db = SessionLocal()
    try:
        assert routing.active_owner(db, AGENT) is None
    finally:
        db.close()


# ─── route_for: sticky, hashed, weight boundaries ────────────────────────────
def test_route_for_is_sticky_and_writes_the_assignment_row():
    exp = _mk_live_canary()
    db = SessionLocal()
    try:
        first = routing.route_for(db, AGENT, "session-A")
        again = routing.route_for(db, AGENT, "session-A")
    finally:
        db.close()
    assert first == again
    assert first.owner_kind == "canary" and first.owner_id == exp.id
    assert _assignments(exp.id) == {"session-A": first.arm}


def test_route_for_honours_an_existing_assignment_over_the_hash():
    exp = _mk_live_canary()
    db = SessionLocal()
    try:
        hashed = routing.choose_arm(routing.active_owner(db, AGENT), "pinned")
        other = "C" if hashed == "T1" else "T1"
        db.add(ExperimentAssignment(workspace_id=DEFAULT_WORKSPACE_ID, owner_kind="canary",
                                    owner_id=exp.id, session_id="pinned", arm=other))
        db.commit()
        route = routing.route_for(db, AGENT, "pinned")
    finally:
        db.close()
    assert route.arm == other


def test_route_for_returns_none_without_an_owner_and_writes_nothing():
    db = SessionLocal()
    try:
        assert routing.route_for(db, AGENT, "s1") is None
        assert db.query(ExperimentAssignment).count() == 0
    finally:
        db.close()


@pytest.mark.parametrize("control_w,treatment_w,expected", [(100, 0, "C"), (0, 100, "T1")])
def test_weight_boundaries_are_deterministic(control_w, treatment_w, expected):
    exp = _mk_live_canary(control_w=control_w, treatment_w=treatment_w)
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, AGENT)
        assigned = routing.assign_batch(owner, [f"s{i}" for i in range(50)], db=db)
    finally:
        db.close()
    assert {a["arm"] for a in assigned.values()} == {expected}
    assert set(_assignments(exp.id).values()) == {expected}


def test_hash_is_deterministic_per_owner_and_session():
    u1 = routing.hash_unit("owner", "session")
    assert u1 == routing.hash_unit("owner", "session")
    assert 0.0 <= u1 < 1.0
    assert routing.hash_unit("owner", "session") != routing.hash_unit("other", "session")


def test_assign_batch_distribution_tracks_the_weights_over_1000_ids():
    exp = _mk_live_canary()
    ids = [f"sess-{i:04d}" for i in range(1000)]
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, AGENT)
        assigned = routing.assign_batch(owner, ids, db=db)
    finally:
        db.close()
    counts = Counter(a["arm"] for a in assigned.values())
    assert 430 <= counts["T1"] <= 570, counts
    rows = _assignments(exp.id)
    assert len(rows) == 1000
    assert rows == {sid: a["arm"] for sid, a in assigned.items()}


def test_assign_batch_is_idempotent_and_keeps_prior_arms():
    exp = _mk_live_canary()
    ids = [f"s{i}" for i in range(30)]
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, AGENT)
        first = routing.assign_batch(owner, ids, db=db)
        # flip one row by hand → the batch must respect it rather than re-hash
        row = db.query(ExperimentAssignment).filter_by(owner_id=exp.id, session_id="s0").one()
        row.arm = "C" if row.arm == "T1" else "T1"
        db.commit()
        second = routing.assign_batch(owner, ids, db=db)
    finally:
        db.close()
    assert second["s0"]["arm"] != first["s0"]["arm"]
    assert {k: v["arm"] for k, v in second.items() if k != "s0"} == {
        k: v["arm"] for k, v in first.items() if k != "s0"}
    assert len(_assignments(exp.id)) == 30


def test_assignment_carries_baggage_and_the_payload_alias():
    exp = _mk_live_canary()
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, AGENT)
        assigned = routing.assign_batch(owner, ["one"], db=db)
    finally:
        db.close()
    a = assigned["one"]
    assert a["qualifier"] == (TREAT if a["arm"] == "T1" else STABLE)
    assert a["baggage"] == (
        f"aws.agentcore.gateway.routing_experiment_arn={exp.id},"
        f"aws.agentcore.gateway.routing_experiment_variant_name={a['arm']}"
    )
    assert a["launchpad_meta"] == {"experiment": exp.id, "variant": a["arm"]}


def test_baggage_percent_encodes_values_and_omits_a_missing_bundle():
    assert routing.baggage_for(None, "own 1", "T1") == (
        "aws.agentcore.gateway.routing_experiment_arn=own%201,"
        "aws.agentcore.gateway.routing_experiment_variant_name=T1"
    )
    assert routing.baggage_for("b,1:2", "o", "C").startswith("launchpad.bundle=b%2C1%3A2,")
    assert routing.bundle_ref_from_baggage(routing.baggage_for("bndl_x:3", "o", "C")) == "bndl_x:3"
    assert routing.bundle_ref_from_baggage("") is None


# ─── reconcile / delete ──────────────────────────────────────────────────────
def test_reconcile_drops_only_planned_sessions_that_never_invoked():
    exp = _mk_live_canary()
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, AGENT)
        routing.assign_batch(owner, ["p1", "p2", "p3"], db=db)
        routing.route_for(db, AGENT, "live-chat")  # a live session, not part of the batch
        dropped = routing.reconcile(
            owner,
            [{"session_id": "p1", "status": "ok"}, {"session_id": "p3", "status": "failed"}],
            planned=["p1", "p2", "p3"], db=db,
        )
    finally:
        db.close()
    assert dropped == ["p2"]
    assert set(_assignments(exp.id)) == {"p1", "p3", "live-chat"}


def test_reconcile_without_a_planned_set_drops_nothing():
    exp = _mk_live_canary()
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, AGENT)
        routing.assign_batch(owner, ["p1", "p2"], db=db)
        assert routing.reconcile(owner, [], db=db) == []
    finally:
        db.close()
    assert set(_assignments(exp.id)) == {"p1", "p2"}


def test_delete_assignments_removes_the_owner_rows_only():
    exp = _mk_live_canary()
    other = _mk_canary(setup={"stable_endpoint": "stable1", "runtime_id": "r"},
                       agent_id="agent2")
    db = SessionLocal()
    try:
        db.add_all([
            ExperimentAssignment(owner_kind="canary", owner_id=exp.id, session_id="a",
                                 arm="C"),
            ExperimentAssignment(owner_kind="canary", owner_id=other.id, session_id="b",
                                 arm="C"),
        ])
        db.commit()
        assert routing.delete_assignments(exp.id, db=db) == 1
    finally:
        db.close()
    assert _assignments(exp.id) == {}
    assert _assignments(other.id) == {"b": "C"}


# ─── ab_results ──────────────────────────────────────────────────────────────
def _result(sid: str, evaluator: str, score: float | None, *, source: str = "run",
            arm: str | None = None) -> EvalResult:
    return EvalResult(id=f"{sid}-{evaluator}-{score}", workspace_id=DEFAULT_WORKSPACE_ID,
                      source=source, run_id="tr123", session_id=sid, evaluator_id=evaluator,
                      level="TRACE", score=score, label=None, arm=arm)


def test_ab_results_has_the_get_ab_test_shape_normalize_reads():
    exp = _mk_live_canary()
    db = SessionLocal()
    try:
        for i in range(6):
            db.add(ExperimentAssignment(owner_kind="canary", owner_id=exp.id,
                                        session_id=f"c{i}", arm="C"))
            db.add(ExperimentAssignment(owner_kind="canary", owner_id=exp.id,
                                        session_id=f"t{i}", arm="T1"))
        control = [0.50, 0.55, 0.45, 0.52, 0.48, 0.51]
        treatment = [0.90, 0.95, 0.85, 0.92, 0.88, 0.91]
        for i in range(6):
            db.add(_result(f"c{i}", "Builtin.Helpfulness", control[i]))
            db.add(_result(f"t{i}", "Builtin.Helpfulness", treatment[i]))
            if i < 3:
                db.add(_result(f"c{i}", "Builtin.Refusal", 0.0))
        # an errored record (no score) and a foreign session never count
        db.add(_result("c0", "Builtin.Helpfulness", None))
        db.add(_result("foreign", "Builtin.Helpfulness", 1.0))
        db.commit()
        owner = routing.active_owner(db, AGENT)
        out = routing.ab_results(db, owner)
    finally:
        db.close()
    assert out["abTestId"] == "local_can"
    assert out["executionStatus"] == "RUNNING"
    metrics = out["results"]["evaluatorMetrics"]
    assert [m["evaluatorId"] for m in metrics] == ["Builtin.Helpfulness", "Builtin.Refusal"]
    helpful = metrics[0]
    assert helpful["controlStats"] == {"name": "C", "mean": pytest.approx(0.5016, abs=1e-3),
                                       "sampleSize": 6}
    (variant,) = helpful["variantResults"]
    assert variant["name"] == "T1"
    assert variant["mean"] == pytest.approx(0.9016, abs=1e-3)
    assert variant["sampleSize"] == 6
    assert variant["pValue"] < 0.001
    assert variant["isSignificant"] is True
    assert variant["percentChange"] == pytest.approx(79.7, abs=0.5)
    refusal = metrics[1]
    assert refusal["controlStats"]["sampleSize"] == 3  # only the three C rows scored
    assert refusal["variantResults"][0] == {
        "name": "T1", "mean": None, "sampleSize": 0, "pValue": None,
        "percentChange": None, "isSignificant": False,
    }
    # the baseline normaliser + verdict read it unchanged
    normalized = routing.normalize_ab_results(out)
    assert normalized[0]["label"] == "Builtin.Helpfulness"
    assert normalized[0]["polarity"] == 1 and normalized[1]["polarity"] == -1
    assert normalized[0]["variants"][0]["isSignificant"] is True


def test_ab_results_p_value_matches_the_welch_table_within_1e3():
    """Arms that differ only by noise → p ≈ 1 (not significant)."""
    exp = _mk_live_canary()
    db = SessionLocal()
    try:
        c = [0.0, 1.0, 0.0, 1.0, 0.0, 1.0]
        t = [1.0, 0.0, 1.0, 0.0, 1.0, 0.0]
        for i in range(6):
            db.add(ExperimentAssignment(owner_kind="canary", owner_id=exp.id,
                                        session_id=f"c{i}", arm="C"))
            db.add(ExperimentAssignment(owner_kind="canary", owner_id=exp.id,
                                        session_id=f"t{i}", arm="T1"))
            db.add(_result(f"c{i}", "Builtin.Correctness", c[i]))
            db.add(_result(f"t{i}", "Builtin.Correctness", t[i]))
        db.commit()
        owner = routing.active_owner(db, AGENT)
        out = routing.ab_results(db, owner)
    finally:
        db.close()
    variant = out["results"]["evaluatorMetrics"][0]["variantResults"][0]
    assert variant["pValue"] == pytest.approx(1.0, abs=1e-3)
    assert variant["isSignificant"] is False


def test_ab_results_without_assignments_or_scores_is_empty_but_well_formed():
    _mk_live_canary()
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, AGENT)
        out = routing.ab_results(db, owner)
    finally:
        db.close()
    assert out["results"] == {"evaluatorMetrics": []}
    assert routing.normalize_ab_results(out) == []


def test_ab_results_small_samples_get_no_p_value():
    exp = _mk_live_canary()
    db = SessionLocal()
    try:
        db.add(ExperimentAssignment(owner_kind="canary", owner_id=exp.id,
                                    session_id="c0", arm="C"))
        db.add(ExperimentAssignment(owner_kind="canary", owner_id=exp.id,
                                    session_id="t0", arm="T1"))
        db.add(_result("c0", "Builtin.Helpfulness", 0.4))
        db.add(_result("t0", "Builtin.Helpfulness", 0.9))
        db.commit()
        owner = routing.active_owner(db, AGENT)
        variant = routing.ab_results(db, owner)["results"]["evaluatorMetrics"][0][
            "variantResults"][0]
    finally:
        db.close()
    assert variant["sampleSize"] == 1 and variant["pValue"] is None
    assert variant["isSignificant"] is False


# ─── relocated pure helpers (formerly agentcore_eval) ────────────────────────
def test_polarity_and_variant_builders_keep_the_baseline_shapes():
    assert routing.evaluator_polarity("Builtin.Refusal") == -1
    assert routing.evaluator_polarity(
        "arn:aws-cn:bedrock-agentcore:::evaluator/Builtin.Refusal") == -1
    assert routing.evaluator_polarity("Builtin.Harmfulness") == 1
    assert routing.evaluator_polarity("cust_abc") == 1
    targets = routing.target_variants("v1", "v2")
    assert [v["weight"] for v in targets] == [90, 10]
    assert targets[0]["variantConfiguration"]["target"]["name"] == "v1"
    assert routing.weights_of(targets) == {"C": 90, "T1": 10}
    reweighted = routing.reweight(targets, control_weight=50, treatment_weight=50)
    assert routing.weights_of(reweighted) == {"C": 50, "T1": 50}
    assert routing.weights_of(targets) == {"C": 90, "T1": 10}  # input untouched


# ── N17-obs387: one observation per session, whichever sources scored it ─────────────


def _row(sid: str, evaluator: str, score: float, *, source: str, trace: str | None = None,
         span: str | None = None, level: str = "TRACE", n: int = 0) -> EvalResult:
    return EvalResult(id=f"{source}-{sid}-{evaluator}-{trace}-{span}-{n}",
                      workspace_id=DEFAULT_WORKSPACE_ID, source=source,
                      run_id="tr123" if source == "run" else None,
                      config_id="cfg" if source == "online" else None, session_id=sid,
                      evaluator_id=evaluator, level=level, trace_id=trace, span_id=span,
                      score=score, label=None)


def test_a_session_scored_by_the_replay_job_and_the_online_config_counts_once():
    # eval-wb's ledger: 36 replay sessions (30 C / 6 T1), each scored by the replay job
    # (source run) AND the arm's online config (source online) — 72 + 72 rows; the 10-per-
    # arm bar was met with 5 real sessions and Welch saw duplicated observations
    exp = _mk_live_canary()
    db = SessionLocal()
    try:
        sessions = [(f"c{i}", "C") for i in range(30)] + [(f"t{i}", "T1") for i in range(6)]
        for sid, arm in sessions:
            db.add(ExperimentAssignment(owner_kind="canary", owner_id=exp.id, session_id=sid,
                                        arm=arm))
            score = 0.833 if arm == "C" else 0.0
            for evaluator in ("Builtin.Helpfulness", "Builtin.Refusal"):
                db.add(_row(sid, evaluator, score, source="run", trace=f"tr-{sid}"))
                # the online scoring of the same session disagrees a little: the run row wins
                db.add(_row(sid, evaluator, 0.5, source="online", trace=f"tr-{sid}"))
        db.commit()
        assert db.query(EvalResult).count() == 144
        out = routing.ab_results(db, routing.active_owner(db, AGENT))
    finally:
        db.close()
    helpful = out["results"]["evaluatorMetrics"][0]
    assert helpful["controlStats"]["sampleSize"] == 30
    assert helpful["variantResults"][0]["sampleSize"] == 6
    assert helpful["controlStats"]["mean"] == pytest.approx(0.833)
    assert helpful["variantResults"][0]["mean"] == pytest.approx(0.0)


def test_per_turn_rows_of_one_session_are_one_observation():
    # a 3-turn session writes 3 TRACE rows: the arm's n is sessions, the observation the
    # session's mean over its turns; a live (online-only) session still counts
    exp = _mk_live_canary()
    db = SessionLocal()
    try:
        for i in range(4):
            for sid, arm in ((f"c{i}", "C"), (f"t{i}", "T1")):
                db.add(ExperimentAssignment(owner_kind="canary", owner_id=exp.id,
                                            session_id=sid, arm=arm))
            for turn, score in enumerate((1.0, 0.0, 0.5)):
                db.add(_row(f"c{i}", "Builtin.Helpfulness", score, source="run",
                            trace=f"c{i}-{turn}", n=turn))
            db.add(_row(f"t{i}", "Builtin.Helpfulness", 1.0, source="online", trace=f"t{i}"))
        db.commit()
        out = routing.ab_results(db, routing.active_owner(db, AGENT))
    finally:
        db.close()
    helpful = out["results"]["evaluatorMetrics"][0]
    assert helpful["controlStats"] == {"name": "C", "mean": pytest.approx(0.5),
                                       "sampleSize": 4}
    assert helpful["variantResults"][0]["sampleSize"] == 4
