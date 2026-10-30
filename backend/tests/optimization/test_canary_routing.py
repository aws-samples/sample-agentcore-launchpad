"""Canary ramp ↔ invoke-chain routing (N12 E3-4, plan §B.8): the local A/B row the
canary actions write is exactly what ``optimization.routing`` splits live traffic
with — weights move the assignment distribution, sticky sessions keep their arm,
complete/rollback stop the split, cleanup removes endpoints + configs + assignments."""

from collections import Counter
from types import SimpleNamespace
from unittest.mock import MagicMock

import app.optimization.canary_service as canary_svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.optimization import routing
from app.optimization.models import ExperimentAssignment, RuntimeCanary
from app.schemas.agent import AgentSpec

ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/subject-abcdefghij"


def _agent() -> str:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="subject", method="zip_runtime",
                      status="active", arn=ARN, resource_id="subject-abcdefghij", version="1",
                      spec=AgentSpec(name="subject", method="zip_runtime",
                                     system_prompt="orig").model_dump())
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


def _canary(agent_id: str, *, ramp_stage: int = 0, verdict: dict | None = None) -> RuntimeCanary:
    c_w, t_w = canary_svc.RAMP_WEIGHTS[ramp_stage]
    rounds = []
    if verdict is not None:
        rounds = [{"ramp_stage": ramp_stage, "weights": {"C": c_w, "T1": t_w},
                   "traffic_attempts": [{"baseline_n": 0, "sent": 5, "failed": 0}],
                   "verdict": verdict}]
    artifacts = {
        "agent_meta": {"id": agent_id, "arn": ARN, "resource_id": "subject-abcdefghij",
                       "runtime_name": "subject"},
        "edited_spec": AgentSpec(name="subject", method="zip_runtime",
                                 system_prompt="edited").model_dump(),
        "setup": {"runtime_id": "subject-abcdefghij", "stable_endpoint": "stableabc123",
                  "treatment_endpoint": "treatabc123", "v_current": "1", "v_candidate": "2",
                  "ramp_stage": ramp_stage, "weights": {"C": c_w, "T1": t_w},
                  "ab_test_id": "local_can",
                  "champion": {"target_name": "stableabc123", "online_eval_id": "oe-c"},
                  "challenger": {"target_name": "treatabc123", "online_eval_id": "oe-t"}},
        "abtest": {"ab_test_id": "local_can", "execution_status": "RUNNING",
                   "variants": routing.target_variants("stableabc123", "treatabc123", c_w, t_w)},
        "rounds": rounds,
    }
    db = SessionLocal()
    try:
        row = RuntimeCanary(workspace_id=DEFAULT_WORKSPACE_ID, name="CAN",
                            champion_agent_id=agent_id, champion_agent_name="subject",
                            challenger_agent_id=agent_id, challenger_agent_name="subject",
                            stage="verdict" if verdict else "setup", artifacts=artifacts)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def _distribution(agent_id: str, ids: list[str]) -> Counter:
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, agent_id)
        assigned = routing.assign_batch(owner, ids, db=db)
    finally:
        db.close()
    return Counter(a["arm"] for a in assigned.values())


def test_ramp_weights_drive_the_assignment_distribution_and_keep_sticky_arms():
    agent_id = _agent()
    row = _canary(agent_id, verdict={"verdict": "treatment-wins", "significant": True})
    first_ids = [f"a{i:04d}" + "x" * 30 for i in range(500)]
    at_90_10 = _distribution(agent_id, first_ids)
    assert 20 <= at_90_10["T1"] <= 90, at_90_10

    canary_svc.act_advance(row.id, lambda m: None, allow_non_significant=False)
    second_ids = [f"b{i:04d}" + "x" * 30 for i in range(500)]
    at_50_50 = _distribution(agent_id, second_ids)
    assert 190 <= at_50_50["T1"] <= 310, at_50_50
    # sessions assigned at 90/10 keep their arm after the ramp
    assert _distribution(agent_id, first_ids) == at_90_10
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, agent_id)
        route = routing.route_for(db, agent_id, first_ids[0])
    finally:
        db.close()
    assert owner.weights == {"C": 50, "T1": 50}
    assert route.qualifier in {"stableabc123", "treatabc123"}
    assert route.qualifier == owner.arm(route.arm).qualifier


def test_complete_stops_routing_and_records_the_candidate_as_production(monkeypatch):
    agent_id = _agent()
    row = _canary(agent_id, ramp_stage=2, verdict={"verdict": "treatment-wins",
                                                   "significant": True})
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    db = SessionLocal()
    try:
        assert routing.active_owner(db, agent_id) is not None
    finally:
        db.close()
    result = canary_svc.act_complete(row.id, lambda m: None, allow_non_significant=False)
    assert result["promoted_version"] == "2" and result["ab_test_status"] == "STOPPED"
    db = SessionLocal()
    try:
        assert routing.active_owner(db, agent_id) is None  # DEFAULT (= candidate) serves
        agent = db.get(Agent, agent_id)
        assert agent.version == "2" and agent.spec["system_prompt"] == "edited"
    finally:
        db.close()


def test_rollback_stops_routing_and_rolls_production_forward(monkeypatch):
    agent_id = _agent()
    row = _canary(agent_id)
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    monkeypatch.setattr(
        canary_svc.canary_infra, "mint_candidate_version",
        lambda **kw: ("1", "7", f"agents/subject/canary/{kw['canary_id']}-restore.zip"),
    )
    result = canary_svc.act_rollback(row.id, lambda m: None)
    assert result["restored_version"] == "7" and result["ab_test_status"] == "STOPPED"
    db = SessionLocal()
    try:
        assert routing.active_owner(db, agent_id) is None
        assert db.get(Agent, agent_id).version == "7"
    finally:
        db.close()


def test_cleanup_removes_endpoints_configs_and_assignments(monkeypatch):
    agent_id = _agent()
    row = _canary(agent_id)
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, agent_id)
        routing.assign_batch(owner, ["s1", "s2", "s3"], db=db)
    finally:
        db.close()
    deleted_endpoints: list[str] = []
    deleted_owners: list[str] = []
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    monkeypatch.setattr(canary_svc.canary_infra, "delete_endpoint_quiet",
                        lambda control, **kw: deleted_endpoints.append(kw["endpoint_name"]))
    monkeypatch.setattr(canary_svc.canary_infra, "wait_endpoints_gone",
                        lambda control, **kw: [])   # N-SCN-obs3: listing is not under test
    monkeypatch.setattr(canary_svc, "_online", lambda: SimpleNamespace(
        delete_for_owner=lambda ws, owner_id: deleted_owners.append(owner_id),
        disable_for_owner=lambda ws, owner_id, db=None: []))  # N17-obs442
    results = canary_svc.act_cleanup(row.id, lambda m: None)
    assert set(deleted_endpoints) == {"stableabc123", "treatabc123"}
    assert deleted_owners == [row.id]
    assert {r["category"]: r["status"] for r in results}["assignments"] == "deleted"
    db = SessionLocal()
    try:
        assert db.query(ExperimentAssignment).filter_by(owner_id=row.id).count() == 0
        assert routing.active_owner(db, agent_id) is None
    finally:
        db.close()
