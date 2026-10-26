"""Gate before traffic: live / candidate endpoints, release records, rollback, waivers."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.dlc import criteria as criteria_svc
from app.dlc import golden as golden_svc
from app.dlc import releases
from app.evaluation.models import EvalDataset, EvalRun
from app.models.dlc import CriteriaSet, Criterion, ReleaseRecord, Waiver
from app.models.ledger import Agent, Workspace

WS = DEFAULT_WORKSPACE_ID


class FakeEndpoints:
    """Stands in for the control client: named endpoints → versions."""

    def __init__(self, live=None):
        self.endpoints = {} if live is None else {"live": live}
        self.calls = []


@pytest.fixture
def ep(monkeypatch):
    fake = FakeEndpoints()

    def point(control, agent, name, version):
        fake.calls.append((name, str(version)))
        fake.endpoints[name] = str(version)
        return {"liveVersion": str(version)}

    monkeypatch.setattr(releases, "point_endpoint", point)
    monkeypatch.setattr(releases, "endpoint_version",
                        lambda control, agent, name: fake.endpoints.get(name))
    monkeypatch.setattr("app.services.agentcore.client.control_client", lambda ctx: fake)
    return fake


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        for model in (ReleaseRecord, Waiver, Criterion, CriteriaSet, EvalRun):
            session.query(model).delete()
        session.query(EvalDataset).filter(EvalDataset.role == "golden").delete()
        session.query(Agent).filter(Agent.name.like("rel-%")).delete()
        ws = session.get(Workspace, WS)
        ws.release_policy = {}
        session.commit()
        yield session
    finally:
        session.rollback()
        ws = session.get(Workspace, WS)
        ws.release_policy = {}
        session.commit()
        session.close()


def _agent(db, mode="live", version="3"):
    agent = Agent(workspace_id=WS, name=f"rel-{version}", method="zip_runtime",
                  status="active", spec={"name": "rel"}, version=version,
                  resource_id="rt_rel-abc", endpoint_mode=mode, owner="dev1")
    db.add(agent)
    db.commit()
    return agent


def _gated(db):
    ws = db.get(Workspace, WS)
    ws.release_policy = {"release_mode": "gated"}
    db.commit()


def _criteria(db, agent, signed=True):
    s = criteria_svc.create_set(db, WS, kind="agent", agent_id=agent.id, name="c", actor="biz")
    criteria_svc.save_draft(db, s, [
        {"key": "R1", "text": "agent never leaks", "dimension": "responsibility",
         "tier": "redline", "executor": {"kind": "evaluator", "evaluator_id": "code-pii"},
         "notes": "n/a:cognition n/a:cost n/a:performance (test fixture)"},
        {"key": "G1", "text": "agent quotes stock", "dimension": "quality", "tier": "gate",
         "threshold": 0.9, "executor": {"kind": "evaluator", "evaluator_id": "code-stock"}},
    ], actor="biz", kind_of=lambda _e: "code")
    criteria_svc.publish(db, s, actor="biz")
    if signed:
        criteria_svc.sign(db, s, actor="owner", note="", is_admin=False)
    db.commit()
    return s


def test_direct_mode_keeps_live_in_lock_step(db, ep):
    agent = _agent(db)
    releases.after_deploy(db, agent, note=None, actor="dev1")
    assert ep.endpoints["live"] == "3" and "candidate" not in ep.endpoints
    assert db.query(ReleaseRecord).count() == 0


def test_default_mode_agents_are_untouched(db, ep):
    agent = _agent(db, mode="default")
    releases.after_deploy(db, agent, note=None, actor="dev1")
    assert ep.calls == []


def test_gated_mode_opens_a_release_on_candidate(db, ep):
    _gated(db)
    ep.endpoints["live"] = "2"
    agent = _agent(db, version="3")
    cset = _criteria(db, agent)
    releases.after_deploy(db, agent, note=None, actor="dev1")
    assert ep.endpoints == {"live": "2", "candidate": "3"}  # production stays on v2
    record = db.query(ReleaseRecord).one()
    assert record.decision == "pending" and record.previous_live_version == "2"
    assert record.criteria_set_id == cset.id and record.requested_by == "dev1"


def test_a_promotion_deploy_stays_in_lock_step_even_when_gated(db, ep):
    _gated(db)
    agent = _agent(db)
    releases.after_deploy(db, agent, note="promotion p1", actor="ops")
    assert ep.endpoints["live"] == "3"


def _golden(db, cset, agent):
    parent = golden_svc.create(db, WS, name="g", criteria_lineage_id=cset.lineage_id,
                               actor="biz")
    splits = golden_svc.splits_of(db, parent)
    item = {"scenario_id": "s1", "turns": [{"input": "hi", "expected_response": "hello"}]}
    golden_svc.add_items(db, splits["regression"], [item], actor="biz")
    golden_svc.add_items(db, splits["holdout"], [dict(item, scenario_id="h1")], actor="biz",
                         allow_holdout=True)
    db.commit()


def _submitted(db):
    submitted = []

    def submit(**kw):
        run = EvalRun(workspace_id=WS, agent_id=kw["agent"].id, agent_name="a",
                      status="queued", endpoint_qualifier=kw["qualifier"],
                      agent_version=kw["agent_version"], split=kw["split"],
                      criteria_set_id=kw["criteria_set_id"], repeats=kw["repeats"])
        db.add(run)
        db.flush()
        submitted.append(kw)
        return run

    return submit, submitted


def _complete(db, record, *, r1_fail=0):
    for rid in record.run_ids:
        run = db.get(EvalRun, rid)
        run.status = "completed"
        run.criteria_summary = {"criteria": {
            "R1": {"kind": "evaluator", "pass": 10 - r1_fail, "fail": r1_fail, "n": 10,
                   "expected": 10, "missing": 0, "inconclusive": 0, "error": 0},
            "G1": {"kind": "evaluator", "pass": 10, "fail": 0, "n": 10, "expected": 10,
                   "missing": 0, "inconclusive": 0, "error": 0},
        }}
    db.commit()


def test_gate_evaluate_sign_and_rollback(db, ep):
    _gated(db)
    ep.endpoints["live"] = "2"
    agent = _agent(db, version="3")
    cset = _criteria(db, agent)
    _golden(db, cset, agent)
    releases.after_deploy(db, agent, note=None, actor="dev1")
    record = db.query(ReleaseRecord).one()
    submit, submitted = _submitted(db)
    run_ids = releases.start_evaluation(db, record, agent, SimpleNamespace(), actor="dev1",
                                        submit=submit)
    assert len(run_ids) == 2 and {s["split"] for s in submitted} == {"regression", "holdout"}
    assert all(s["qualifier"] == "candidate" and s["agent_version"] == "3" for s in submitted)
    assert releases.evaluate(db, record)["status"] == "evaluating"
    _complete(db, record)
    out = releases.evaluate(db, record)
    assert out["report"]["verdict"] == "PASS", out["report"]
    with pytest.raises(AppError) as exc:
        releases.sign(db, record, agent, ep, actor="dev1")
    assert exc.value.code == "release.self_sign"
    releases.sign(db, record, agent, ep, actor="ops")
    assert ep.endpoints["live"] == "3" and record.decision == "released"
    rolled = releases.rollback(db, agent, ep, actor="ops")
    assert rolled["live_version"] == "2" and ep.endpoints["live"] == "2"


def test_an_interrupted_gate_run_is_invalid_and_can_be_submitted_again(db, ep):
    """A backend restart fails a gate run before it is scored: the report says INVALID
    (evidence incomplete, not BLOCKED), and the same candidate can be sent to the gate
    again; the fresh runs replace the old ones and decide it."""
    _gated(db)
    ep.endpoints["live"] = "2"
    agent = _agent(db, version="3")
    cset = _criteria(db, agent)
    _golden(db, cset, agent)
    releases.after_deploy(db, agent, note=None, actor="dev1")
    record = db.query(ReleaseRecord).one()
    submit, _ = _submitted(db)
    first = releases.start_evaluation(db, record, agent, SimpleNamespace(), actor="dev1",
                                      submit=submit)
    _complete(db, record)
    lost = db.get(EvalRun, first[1])
    lost.status, lost.criteria_summary = "failed", None
    db.commit()
    out = releases.evaluate(db, record)
    assert out["report"]["verdict"] == "INVALID", out["report"]
    assert record.decision == "invalid"
    again = releases.start_evaluation(db, record, agent, SimpleNamespace(), actor="dev1",
                                      submit=submit)
    assert set(again).isdisjoint(first) and record.run_ids == again
    assert record.decision == "evaluating"
    _complete(db, record)
    assert releases.evaluate(db, record)["report"]["verdict"] == "PASS"


def test_a_red_line_violation_blocks_the_release(db, ep):
    _gated(db)
    agent = _agent(db)
    cset = _criteria(db, agent)
    _golden(db, cset, agent)
    releases.after_deploy(db, agent, note=None, actor="dev1")
    record = db.query(ReleaseRecord).one()
    submit, _ = _submitted(db)
    releases.start_evaluation(db, record, agent, SimpleNamespace(), actor="dev1", submit=submit)
    _complete(db, record, r1_fail=1)
    assert releases.evaluate(db, record)["report"]["verdict"] == "BLOCKED"
    assert record.decision == "blocked"
    with pytest.raises(AppError) as exc:
        releases.sign(db, record, agent, ep, actor="ops")
    assert exc.value.code == "release.gate_not_passed"


def test_unsigned_criteria_make_the_report_invalid(db, ep):
    _gated(db)
    agent = _agent(db)
    cset = _criteria(db, agent, signed=False)
    _golden(db, cset, agent)
    releases.after_deploy(db, agent, note=None, actor="dev1")
    record = db.query(ReleaseRecord).one()
    submit, _ = _submitted(db)
    releases.start_evaluation(db, record, agent, SimpleNamespace(), actor="dev1", submit=submit)
    _complete(db, record)
    report = releases.evaluate(db, record)["report"]
    assert report["verdict"] == "INVALID"
    assert any("not signed" in i for i in report["provenance"]["issues"])


def test_waiver_rules(db, ep):
    agent = _agent(db)
    _criteria(db, agent)
    soon = datetime.now(UTC) + timedelta(days=7)
    with pytest.raises(AppError) as exc:
        releases.request_waiver(db, workspace_id=WS, agent_id=agent.id, criterion_key="R1",
                                actual=0.9, threshold=None, reason="x", risk_owner="r",
                                compensating_control="", expires_on=soon, actor="dev1")
    assert exc.value.code == "waiver.redline"
    with pytest.raises(AppError) as exc:
        releases.request_waiver(db, workspace_id=WS, agent_id=agent.id, criterion_key="G1",
                                actual=0.8, threshold=0.9, reason="x", risk_owner="r",
                                compensating_control="", actor="dev1",
                                expires_on=datetime.now(UTC) + timedelta(days=60))
    assert exc.value.code == "waiver.too_long"
    waiver = releases.request_waiver(db, workspace_id=WS, agent_id=agent.id,
                                     criterion_key="G1", actual=0.8, threshold=0.9,
                                     reason="vendor outage", risk_owner="risk-lead",
                                     compensating_control="manual review", expires_on=soon,
                                     actor="dev1")
    with pytest.raises(AppError):
        releases.decide_waiver(db, waiver, actor="dev1", approve=True)
    releases.decide_waiver(db, waiver, actor="risk-lead", approve=True)
    assert waiver.status == "approved"
    assert releases.waiver_out(waiver)["active"] is True


def test_gateable_rules():
    agent = Agent(method="harness", spec={}, resource_id="h", version="1")
    assert releases.gateable(agent) == (True, None)
    assert releases.gateable(Agent(method="discovered", spec={}, resource_id="x",
                                   version="1"))[0] is False
    assert releases.gateable(Agent(method="zip_runtime", spec={"protocol": "a2a"},
                                   resource_id="x", version="1"))[0] is False


def test_seeding_curates_all_three_splits_and_seals_the_holdout(db):
    agent = _agent(db)
    cset = _criteria(db, agent)
    parent = golden_svc.create(db, WS, name="seeded", criteria_lineage_id=cset.lineage_id,
                               actor="biz")
    items = [
        {"scenario_id": f"s{n}", "turns": [{"input": f"q{n}", "expected_response": f"a{n}"}],
         "metadata": {"dlc": {"case_tier": "known_good" if n % 2 else "adversarial"}}}
        for n in range(10)
    ]
    counts = golden_svc.seed(db, parent, items, actor="biz")
    db.commit()
    assert sum(counts.values()) == 10
    assert counts["dev"] >= counts["regression"] >= counts["holdout"] >= 1
    splits = golden_svc.splits_of(db, parent)
    # the mix is stratified: every split sees adversarial cases, not just the first slice
    for split in splits.values():
        tiers = {golden_svc.dlc_meta(i).get("case_tier") for i in split.items}
        assert "adversarial" in tiers
    # sealed: curation happens once, so nothing can later be tuned against the holdout
    with pytest.raises(AppError) as exc:
        golden_svc.seed(db, parent, [{"scenario_id": "late", "turns": [{"input": "q"}]}],
                        actor="biz")
    assert exc.value.code == "golden.holdout_sealed"


def test_seeding_honours_an_explicit_split_and_refuses_an_unknown_one(db):
    agent = _agent(db)
    cset = _criteria(db, agent)
    parent = golden_svc.create(db, WS, name="named", criteria_lineage_id=cset.lineage_id,
                               actor="biz")
    counts = golden_svc.seed(db, parent, [
        {"split": "holdout", "scenario_id": "h1",
         "turns": [{"input": "q", "expected_response": "a"}]},
        {"split": "dev", "scenario_id": "d1",
         "turns": [{"input": "q", "expected_response": "a"}]},
    ], actor="biz")
    db.commit()
    assert counts == {"dev": 1, "regression": 0, "holdout": 1}
    other = golden_svc.create(db, WS, name="bad", criteria_lineage_id=cset.lineage_id,
                              actor="biz")
    with pytest.raises(AppError) as exc:
        golden_svc.seed(db, other, [{"split": "nope", "scenario_id": "x",
                                     "turns": [{"input": "q"}]}], actor="biz")
    assert exc.value.code == "golden.bad_split"
    with pytest.raises(AppError) as exc:
        golden_svc.seed(db, other, [], actor="biz")
    assert exc.value.code == "golden.seed_empty"


def test_named_endpoints_are_deleted_before_the_resource(db, monkeypatch):
    """AgentCore will not delete a runtime that still has endpoints, and an orphaned
    `live` endpoint keeps serving a version nobody can see."""
    from app.dlc import releases as release_svc

    agent = _agent(db)
    agent.method = "harness"
    db.commit()
    deleted = []
    monkeypatch.setattr("app.services.agentcore.harness.delete_harness_endpoint",
                        lambda control, rid, name: deleted.append((rid, name)) or True)
    monkeypatch.setattr(release_svc, "endpoint_status", lambda a, c, name: None)
    out = release_svc.delete_endpoints(agent, object())
    # candidate first, then live: the one serving traffic goes last
    assert [name for _, name in deleted] == ["candidate", "live"]
    assert [row["status"] for row in out] == ["deleted", "deleted"]


def _gone_after(reads):
    """An `endpoint_status` that reports DELETING for N reads, then None (gone)."""
    calls = {"n": 0}

    def fn(agent, control, name):
        calls["n"] += 1
        return "DELETING" if calls["n"] <= reads else None

    return fn


def test_endpoint_deletion_waits_for_aws_to_finish(db, monkeypatch):
    """DeleteHarnessEndpoint is asynchronous; returning before the endpoint is really
    gone made the agent delete fail with "has endpoints that must be deleted first"."""
    from app.dlc import releases as release_svc

    agent = _agent(db)
    agent.method = "harness"
    db.commit()
    waits: list[float] = []
    monkeypatch.setattr(release_svc, "_sleep", waits.append)
    monkeypatch.setattr("app.services.agentcore.harness.delete_harness_endpoint",
                        lambda control, rid, name: True)
    # each endpoint is still there for two reads, then gone
    monkeypatch.setattr(release_svc, "endpoint_status", _gone_after(2))
    out = release_svc.delete_endpoints(agent, object())
    assert [row["status"] for row in out] == ["deleted", "deleted"]
    assert waits, "it must actually wait, not just issue the delete"


def test_an_endpoint_already_deleting_is_still_waited_out(db, monkeypatch):
    from app.dlc import releases as release_svc

    agent = _agent(db)
    agent.method = "harness"
    db.commit()
    monkeypatch.setattr(release_svc, "_sleep", lambda s: None)

    def conflict(control, rid, name):
        raise type("ConflictException", (Exception,), {})("already DELETING")

    monkeypatch.setattr("app.services.agentcore.harness.delete_harness_endpoint", conflict)
    monkeypatch.setattr(release_svc, "endpoint_status", _gone_after(1))
    out = release_svc.delete_endpoints(agent, object())
    assert [row["status"] for row in out] == ["deleted", "deleted"]


def test_a_stuck_endpoint_times_out_rather_than_hanging(db, monkeypatch):
    from app.dlc import releases as release_svc

    agent = _agent(db)
    agent.method = "harness"
    db.commit()
    monkeypatch.setattr(release_svc, "_sleep", lambda s: None)
    monkeypatch.setattr("app.services.agentcore.harness.delete_harness_endpoint",
                        lambda control, rid, name: True)
    monkeypatch.setattr(release_svc, "endpoint_status", lambda a, c, name: "DELETING")
    out = release_svc.delete_endpoints(agent, object(), timeout_s=0.01)
    # reported, not raised: a stuck endpoint must not make an agent undeletable
    assert [row["status"] for row in out] == ["timeout", "timeout"]


def test_a_missing_endpoint_is_absent_not_an_error(db, monkeypatch):
    from app.dlc import releases as release_svc

    agent = _agent(db)

    class _Gone(Exception):
        pass

    _Gone.__name__ = "ResourceNotFoundException"

    def boom(control, *, runtime_id, endpoint_name):
        raise _Gone("gone")

    monkeypatch.setattr("app.services.agentcore.runtime.delete_runtime_endpoint", boom)
    monkeypatch.setattr(release_svc, "_sleep", lambda s: None)
    out = release_svc.delete_endpoints(agent, object())
    assert [row["status"] for row in out] == ["absent", "absent"]


def test_an_ungated_agent_has_no_endpoints_to_delete(db):
    from app.dlc import releases as release_svc

    # endpoint_mode "default" = a single DEFAULT endpoint AWS owns; nothing to delete
    assert release_svc.delete_endpoints(_agent(db, mode="default"), object()) == []


def test_a_still_deleting_harness_defers_instead_of_refusing_the_delete(db, monkeypatch):
    """AgentCore refuses DeleteHarness while endpoints are DELETING. Refusing the whole
    request would leave the operator retrying by hand against an async state machine."""
    from app.core.errors import AppError
    from app.deployer import harness as harness_method
    from app.dlc import releases as release_svc

    agent = _agent(db)
    agent.method = "harness"
    db.commit()

    class _Client:
        class exceptions:  # noqa: N801 - mirrors botocore's shape
            ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
            ConflictException = type("ConflictException", (Exception,), {})

    client = _Client()
    monkeypatch.setattr(harness_method, "control_client", lambda ws: client)
    monkeypatch.setattr(release_svc, "delete_endpoints", lambda a, c, **kw: [])
    monkeypatch.setattr("app.services.agentcore.harness.delete_harness",
                        lambda c, rid: (_ for _ in ()).throw(
                            _Client.exceptions.ConflictException("has endpoints")))
    ws = SimpleNamespace(resources={}, region="us-east-1")
    with pytest.raises(AppError) as exc:
        harness_method.delete_agent_resources(agent, ws)
    assert exc.value.code == "agent.teardown_pending"
    assert "automatically" in exc.value.message  # it says who finishes the job


@pytest.mark.parametrize("module", ["zip_runtime", "container", "byoc"])
def test_a_runtime_still_holding_deleting_endpoints_defers_like_a_harness(
        db, monkeypatch, module):
    """A gated runtime agent whose live/candidate endpoints outlive the delete wait
    gets ConflictException from DeleteAgentRuntime: that must defer to the sweep
    (teardown_pending), not surface as a 409 the operator can only retry by hand."""
    import importlib

    from app.core.errors import AppError
    from app.dlc import releases as release_svc

    method = importlib.import_module(f"app.deployer.{module}")
    agent = _agent(db)
    agent.method = {"zip_runtime": "zip_runtime"}.get(module, module)
    db.commit()

    class _Client:
        class exceptions:  # noqa: N801 - mirrors botocore's shape
            ResourceNotFoundException = type("ResourceNotFoundException", (Exception,), {})
            ConflictException = type("ConflictException", (Exception,), {})

    client = _Client()
    monkeypatch.setattr(method, "control_client", lambda ws: client)
    monkeypatch.setattr(release_svc, "delete_endpoints", lambda a, c, **kw: [])
    monkeypatch.setattr("app.services.agentcore.runtime.delete_runtime",
                        lambda c, rid: (_ for _ in ()).throw(
                            _Client.exceptions.ConflictException("has endpoints")))
    ws = SimpleNamespace(resources={}, region="us-east-1")
    with pytest.raises(AppError) as exc:
        method.delete_agent_resources(agent, ws)
    assert exc.value.code == "agent.teardown_pending"
