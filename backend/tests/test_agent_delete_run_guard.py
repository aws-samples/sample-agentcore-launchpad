"""N17-obs18 (round 17, r17-e cross walk `agent_x_run`; fix cn/r17-f): `DELETE /api/agents/{id}`
answered 200 while an evaluation run on the agent was `queued`; the run kept naming the
deleted agent, `stop` still answered 202, and at dispatch the worker — which never re-read the
agent — completed it against the torn-down Runtime, recording a scorecard version for a
deleted agent. RULING (a): the delete is refused with the canary guard's envelope,
`409 agent.in_use {canaries, runs: [{id, name, status}]}`, while a run on the agent is ACTIVE
(`evaluation.service.ACTIVE_STATUSES`, the definition that blocks dataset deletes) — members
and administrators alike, nothing written; terminal runs never block; a re-publish is NOT
blocked by a run (a queued run follows the version live at dispatch, §22). (b) belt: the
worker re-reads the agent at dispatch and fails the run `agent.not_found` — no `invoking`
flip, no version stamp — when the agent is deleted or gone. Contract: agents.md §1.3,
evaluation-engine.md §23, api.md."""
# ruff: noqa: F401, F811  — pytest fixtures are imported by name from the canary-guard suite
from __future__ import annotations

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import service as svc
from app.evaluation.models import EvalRun
from app.models.ledger import Agent
from tests.conftest import ws_ctx
from tests.graph.executor import ledger_digest
from tests.test_agent_delete_canary_guard import (
    SPEC,
    _canary,
    _status,
    active_agent,
    no_real_deploy,
    teardown_stubs,
)

TERMINAL_RUNS = ("completed", "failed", "stopped")


def _run(agent_id: str, *, status: str = "queued", name: str = "guard-run") -> str:
    db = SessionLocal()
    try:
        row = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, agent_name="agent",
                      name=name, status=status, mode="evaluators")
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _row(run_id: str) -> EvalRun:
    db = SessionLocal()
    try:
        row = db.get(EvalRun, run_id)
        db.expunge(row)
        return row
    finally:
        db.close()


@pytest.mark.parametrize("status", svc.ACTIVE_STATUSES)
def test_delete_is_409_agent_in_use_while_a_run_is_active(client, active_agent, status):
    run_id = _run(active_agent, status=status)
    before = ledger_digest()

    res = client.delete(f"/api/agents/{active_agent}")

    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "agent.in_use"
    assert body["detail"]["canaries"] == []
    assert body["detail"]["runs"] == [{"id": run_id, "name": "guard-run", "status": status}]
    assert "evaluation run" in body["message"] and "removing the agent" in body["message"]
    assert ledger_digest() == before  # no teardown, the run and the agent untouched
    assert _status(Agent, active_agent) == "active" and _status(EvalRun, run_id) == status


def test_both_referrer_kinds_are_listed_together(client, active_agent):
    canary_id = _canary(active_agent)
    run_id = _run(active_agent)
    body = client.delete(f"/api/agents/{active_agent}").json()
    assert [c["id"] for c in body["detail"]["canaries"]] == [canary_id]
    assert [r["id"] for r in body["detail"]["runs"]] == [run_id]
    assert "canary" in body["message"] and "evaluation run" in body["message"]


@pytest.mark.parametrize("status", TERMINAL_RUNS)
def test_a_terminal_run_never_blocks_the_delete(client, active_agent, teardown_stubs, status):
    _run(active_agent, status=status)
    res = client.delete(f"/api/agents/{active_agent}")
    assert res.status_code == 200, res.text
    assert _status(Agent, active_agent) == "deleted"


def test_stopping_the_run_unblocks_the_delete(client, active_agent, teardown_stubs):
    """The live re-test sequence: 409 listing the run → stop → 200."""
    run_id = _run(active_agent)
    assert client.delete(f"/api/agents/{active_agent}").status_code == 409
    db = SessionLocal()
    try:
        db.get(EvalRun, run_id).status = "stopped"
        db.commit()
    finally:
        db.close()
    assert client.delete(f"/api/agents/{active_agent}").status_code == 200
    assert _status(Agent, active_agent) == "deleted"


def test_a_redeploy_is_not_blocked_by_an_active_run(client, active_agent):
    """§22: a queued run evaluates the version live at dispatch — the re-publish asks the
    guard for canaries only."""
    _run(active_agent)
    res = client.post(f"/api/agents/{active_agent}/redeploy",
                      json={**SPEC, "system_prompt": "hi v2"})
    assert res.status_code == 202, res.text


def _plan(run_id: str, agent_id: str | None) -> svc.RunPlan:
    return svc.RunPlan(run_id=run_id, job_type="batch_eval", target={"agent_id": agent_id},
                       sessions=[{"session_id": "s1", "prompt": "hi"}], replay=True, wait={},
                       agent_id=agent_id)


def test_the_worker_fails_a_run_whose_agent_was_deleted_before_dispatch(active_agent):
    run_id = _run(active_agent)
    db = SessionLocal()
    try:
        db.get(Agent, active_agent).status = "deleted"  # a delete that landed anyway
        db.commit()
    finally:
        db.close()

    svc.execute_run(run_id, workspace=ws_ctx(), plan=_plan(run_id, active_agent))

    row = _row(run_id)
    assert row.status == "failed"
    assert row.error.startswith("agent.not_found")
    # never dispatched: no job, no `invoking`, no version stamped for a deleted agent
    assert row.batch_eval_id is None and row.agent_snapshot_seq is None


def test_the_worker_fails_a_run_whose_agent_row_is_gone(active_agent):
    run_id = _run(active_agent)
    db = SessionLocal()
    try:
        db.delete(db.get(Agent, active_agent))
        db.commit()
    finally:
        db.close()
    svc.execute_run(run_id, workspace=ws_ctx(), plan=_plan(run_id, active_agent))
    row = _row(run_id)
    assert row.status == "failed" and row.error.startswith("agent.not_found")


def test_a_log_source_run_has_no_agent_to_check(active_agent):
    """`_check_agent_present` passes a run without an agent (telemetry-only target)."""
    run_id = _run(active_agent)
    svc._check_agent_present(run_id, _plan(run_id, None))  # no raise
