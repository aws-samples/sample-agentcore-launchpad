"""N17-obs13 — agents.md §1.2: a Runtime canary's `complete` / `rollback` publish the champion's
live version, so while the agent's deploy job is `queued` / `running` they answer the shared
`409 agent.deploy_in_progress {job_id, stage, job_status}` (the envelope `DELETE /api/agents`
answers since N16-obs83) and write nothing; the split-only actions keep going. The route
answers the 409 before the 202 (`tests/graph/test_graph_crossgraph.py` drives that); this file
is the service-level belt: `refuse_publish_during_deploy` itself and the two `act_*` entry
points, which re-check under their own session before the A/B stop."""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.deployer.pipeline import create_deployment
from app.models.ledger import Agent, Job
from app.optimization import canary_service
from app.optimization.models import RuntimeCanary
from tests.graph.executor import ledger_digest

SPLIT_ACTIONS = ("setup", "traffic", "verdict", "advance", "cleanup")


def _agent(name: str) -> Agent:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="zip_runtime",
                      status="active", spec={"name": name, "system_prompt": "hi v1"},
                      arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/r",
                      resource_id="r", version="1")
        db.add(agent)
        db.commit()
        db.expunge(agent)
        return agent
    finally:
        db.close()


def _canary(agent: Agent) -> str:
    db = SessionLocal()
    try:
        row = RuntimeCanary(
            workspace_id=DEFAULT_WORKSPACE_ID, name=f"CANARY-{agent.name}",
            champion_agent_id=agent.id, champion_agent_name=agent.name,
            challenger_agent_id=agent.id, challenger_agent_name=agent.name,
            status="running", stage="ready",
            artifacts={
                "agent_meta": {"id": agent.id, "arn": agent.arn, "resource_id": agent.resource_id,
                               "runtime_name": agent.name},
                "edited_spec": {**agent.spec, "system_prompt": "candidate prompt"},
                "setup": {"ab_test_id": "ab1", "v_candidate": "2", "ramp_stage": 2},
                "rounds": [],
            },
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _queue_update_job(agent_id: str) -> str:
    """An update deploy job that stays `queued` (never started): the agent is owned."""
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        agent.status = "deploying"
        _deployment, job = create_deployment(db, agent, mode="update")
        return job.id
    finally:
        db.close()


def _finish_job(job_id: str, status: str) -> None:
    db = SessionLocal()
    try:
        db.get(Job, job_id).status = status
        db.commit()
    finally:
        db.close()


def _row(canary_id: str) -> RuntimeCanary:
    db = SessionLocal()
    try:
        row = db.get(RuntimeCanary, canary_id)
        db.expunge(row)
        return row
    finally:
        db.close()


@pytest.mark.parametrize("action", sorted(canary_service.PUBLISH_ACTIONS))
def test_publish_actions_are_409_while_the_job_is_live_and_write_nothing(action):
    agent = _agent(f"guard-{action}")
    canary_id = _canary(agent)
    job_id = _queue_update_job(agent.id)
    before = ledger_digest()

    with pytest.raises(AppError) as exc:
        canary_service.refuse_publish_during_deploy(_row(canary_id), action)

    assert exc.value.status_code == 409 and exc.value.code == "agent.deploy_in_progress"
    assert exc.value.detail == {"job_id": job_id, "stage": "generate", "job_status": "queued"}
    assert f"{action} the canary" in exc.value.message
    assert ledger_digest() == before  # the refusal is read-only


@pytest.mark.parametrize("action", SPLIT_ACTIONS)
def test_split_only_actions_are_not_gated_by_the_job(action):
    agent = _agent(f"split-{action}")
    canary_id = _canary(agent)
    _queue_update_job(agent.id)
    canary_service.refuse_publish_during_deploy(_row(canary_id), action)  # no raise


@pytest.mark.parametrize("terminal", ["succeeded", "failed", "cancelled"])
def test_a_terminal_job_hands_the_agent_back(terminal):
    agent = _agent(f"terminal-{terminal}")
    canary_id = _canary(agent)
    job_id = _queue_update_job(agent.id)
    _finish_job(job_id, terminal)
    for action in canary_service.PUBLISH_ACTIONS:
        canary_service.refuse_publish_during_deploy(_row(canary_id), action)


def test_no_job_at_all_passes():
    agent = _agent("no-job")
    canary_id = _canary(agent)
    canary_service.refuse_publish_during_deploy(_row(canary_id), "complete")
    canary_service.refuse_publish_during_deploy(_row(canary_id), "rollback")


@pytest.mark.parametrize("act, kwargs", [
    (canary_service.act_complete, {"allow_non_significant": True}),
    (canary_service.act_rollback, {}),
])
def test_the_actions_themselves_refuse_before_the_ab_stop(monkeypatch, act, kwargs):
    """The thread-level belt: a job that landed between the route's check and the action
    thread is still refused inside `act_complete` / `act_rollback` — before `_stop_ab_test`,
    before any ledger write, before the data plane."""
    agent = _agent(f"act-{act.__name__}")
    canary_id = _canary(agent)
    _queue_update_job(agent.id)
    touched: list[str] = []
    monkeypatch.setattr(canary_service, "_stop_ab_test",
                        lambda *a, **k: touched.append("stop") or {})
    monkeypatch.setattr(canary_service, "assert_verdict_allows",
                        lambda *a, **k: touched.append("verdict"))
    monkeypatch.setattr(canary_service, "control_client",
                        lambda *a, **k: touched.append("control"))
    before = ledger_digest()

    with pytest.raises(AppError) as exc:
        act(canary_id, lambda _m: None, **kwargs)

    assert exc.value.code == "agent.deploy_in_progress" and exc.value.status_code == 409
    assert touched == []
    assert ledger_digest() == before
    row = _row(canary_id)
    assert row.status == "running" and row.stage == "ready"
