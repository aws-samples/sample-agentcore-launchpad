"""N17-obs16 belt (agents.md §1.2): a canary's `complete` / `rollback` refuse with
`409 canary.stale_base {setup_version, current_version}` once the champion's ledger version
moved past the one `setup` recorded (`artifacts.setup.agent_version`) — the candidate was
built on a base that is no longer production. Route pre-flight (a plain 409) and the action
thread (`act_complete` / `act_rollback`, before `_stop_ab_test`) both apply it; a canary set
up before the key existed is never stale by this rule."""
from __future__ import annotations

import pytest

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent
from app.optimization import canary_service
from tests.graph.executor import ledger_digest
from tests.test_canary_deploy_guard import _agent, _canary, _row


def _bump_version(agent_id: str, version: str) -> None:
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).version = version
        db.commit()
    finally:
        db.close()


def _with_setup_version(canary_id: str, version) -> None:
    db = SessionLocal()
    try:
        from app.optimization.models import RuntimeCanary

        row = db.get(RuntimeCanary, canary_id)
        setup = dict(row.artifacts["setup"])
        setup["agent_version"] = version
        row.artifacts = {**row.artifacts, "setup": setup}
        db.commit()
    finally:
        db.close()


def test_stale_base_is_none_while_the_version_setup_recorded_is_production():
    agent = _agent("stale-same")
    canary_id = _canary(agent)
    _with_setup_version(canary_id, agent.version)
    db = SessionLocal()
    try:
        assert canary_service.stale_base(_row(canary_id), db.get(Agent, agent.id)) is None
    finally:
        db.close()
    canary_service.refuse_stale_base(_row(canary_id), "complete")
    canary_service.refuse_stale_base(_row(canary_id), "rollback")


@pytest.mark.parametrize("action", sorted(canary_service.PUBLISH_ACTIONS))
def test_publishes_are_409_once_the_champion_moved(action):
    agent = _agent(f"stale-{action}")
    canary_id = _canary(agent)
    _with_setup_version(canary_id, "1")
    _bump_version(agent.id, "3")  # a re-publish finished after setup
    before = ledger_digest()

    with pytest.raises(AppError) as exc:
        canary_service.refuse_stale_base(_row(canary_id), action)

    assert exc.value.status_code == 409 and exc.value.code == "canary.stale_base"
    assert exc.value.detail == {"setup_version": "1", "current_version": "3", "action": action}
    assert ledger_digest() == before


@pytest.mark.parametrize("action", ["setup", "traffic", "verdict", "advance", "cleanup"])
def test_split_only_actions_ignore_the_base(action):
    agent = _agent(f"stale-split-{action}")
    canary_id = _canary(agent)
    _with_setup_version(canary_id, "1")
    _bump_version(agent.id, "3")
    canary_service.refuse_stale_base(_row(canary_id), action)  # no raise


def test_a_canary_set_up_before_the_key_existed_is_never_stale():
    agent = _agent("stale-legacy")
    canary_id = _canary(agent)  # the test row's setup carries no `agent_version`
    _bump_version(agent.id, "9")
    canary_service.refuse_stale_base(_row(canary_id), "complete")
    canary_service.refuse_stale_base(_row(canary_id), "rollback")


@pytest.mark.parametrize("act, kwargs", [
    (canary_service.act_complete, {"allow_non_significant": True}),
    (canary_service.act_rollback, {}),
])
def test_the_actions_refuse_a_stale_base_before_the_ab_stop(monkeypatch, act, kwargs):
    agent = _agent(f"stale-act-{act.__name__}")
    canary_id = _canary(agent)
    _with_setup_version(canary_id, agent.version)
    _bump_version(agent.id, "7")
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

    assert exc.value.code == "canary.stale_base" and exc.value.status_code == 409
    assert touched == [] and ledger_digest() == before
    assert _row(canary_id).status == "running"


def test_the_route_answers_a_plain_409_for_a_stale_rollback(client):
    """`rollback` passes every earlier route gate on any running canary, so the stale-base
    pre-flight is the answer — before the 202 and before the action thread."""
    agent = _agent("stale-route")
    canary_id = _canary(agent)
    _with_setup_version(canary_id, agent.version)
    _bump_version(agent.id, "4")
    before = ledger_digest()

    res = client.post(f"/api/runtime-canaries/{canary_id}/action", json={"action": "rollback"})

    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "canary.stale_base"
    assert body["detail"] == {"setup_version": "1", "current_version": "4", "action": "rollback"}
    assert ledger_digest() == before
    row = _row(canary_id)
    assert row.status == "running" and row.running_action is None
