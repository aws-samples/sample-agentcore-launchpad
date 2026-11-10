# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_policy_deploy_narrowing
"""Stream A3 (part 1) — the deploy-time decision: which agents render the principal carrier
(``policy_enforcement.principal_carrier_for``) and the one carrier decision a deploy job makes
(flag off = unchanged). Global parity: no runtime guard, no narrowing — the Gateway decides."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.deployer import zip_runtime as zr
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import policy_enforcement
from tests.test_policy_deploy_narrowing import (  # noqa: F401 — shared fixtures
    PERMIT_HR,
    WS,
    _agent,
    _gateway_spec,
    _publish,
    _set_mode,
    engine,
    s3,
    workspace,
)


def _flags(monkeypatch, *, interceptor: bool) -> None:
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", interceptor)


def _carrier(agent_id: str) -> bool:
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        return policy_enforcement.principal_carrier_for(db, row, AgentSpec(**row.spec))
    finally:
        db.close()


def test_a_governed_strands_agent_renders_the_carrier(monkeypatch):
    agent_id = _agent()
    _flags(monkeypatch, interceptor=False)
    assert _carrier(agent_id) is False
    _flags(monkeypatch, interceptor=True)
    assert _carrier(agent_id) is True


@pytest.mark.parametrize("make, why", [
    (lambda: _agent(_gateway_spec() | {"tools": []}, agent_id="ag2"), "no gateway tools"),
    (lambda: _agent(agent_id="ag3", system_key="architect", name="launchpad-architect"),
     "exempt preset"),
    (lambda: _agent(_gateway_spec(protocol="a2a"), agent_id="ag4"), "A2A server"),
])
def test_agents_outside_the_strands_carrier_path(monkeypatch, make, why):
    agent_id = make()
    _flags(monkeypatch, interceptor=True)
    assert _carrier(agent_id) is False, why


HUB_GATEWAY = "gw-hub-1"


def _governs(monkeypatch, on: bool) -> None:
    """The hub-side publish state (review F1, N17-obs490): the engine holds the hub's current
    set and the bundle write succeeded — `policy_gateway_publish.interceptor_governs`."""
    from app.services import policy_gateway_publish

    monkeypatch.setattr(policy_gateway_publish, "interceptor_governs", lambda db, ws: on)


def _attached(state: bool | None) -> None:
    """The workspace gateway's persisted attach state: None = never attached (no row)."""
    from app.services import policy_gateway_state
    from tests.conftest import set_default_resources

    set_default_resources({"artifacts_bucket": "launchpad-artifacts-test",
                           "gateway_id": HUB_GATEWAY})
    if state is None:
        return
    db = SessionLocal()
    try:
        policy_gateway_state.record_attach(
            db, HUB_GATEWAY, "arn:aws-cn:lambda:cn-northwest-1:111122223333:function:lp-pi")
        if state is False:
            policy_gateway_state.record_detach(db, HUB_GATEWAY)
    finally:
        db.close()


def test_the_generator_renders_the_carrier_only_when_asked():
    spec = AgentSpec(**_gateway_spec())
    plain, label = zr._generate_code(spec)
    carried, carried_label = zr._generate_code(spec, principal_carrier=True)
    assert "launchpad_set_principal" not in plain and label == "strands template"
    assert "turn = launchpad_principal_turn(payload, turn)" in carried
    assert carried_label == "strands template + principal carrier"


def test_a_governed_self_built_harness_renders_the_carrier_too(monkeypatch):
    agent_id = _agent(_gateway_spec(method="harness"), agent_id="ag5", name="hr-harness")
    _flags(monkeypatch, interceptor=False)
    assert _carrier(agent_id) is False
    _flags(monkeypatch, interceptor=True)
    assert _carrier(agent_id) is True


def test_one_carrier_decision_per_job_for_the_policy_context_and_the_render(
        monkeypatch, engine, s3, workspace):
    """Re-review nit b: the job computes (carrier, interceptor decides) ONCE — an attach
    landing between the deploy-time policy context and the render can never make the two
    disagree about the carrier."""
    from types import SimpleNamespace

    agent_id = _agent()
    _publish(workspace, "permit_hr", PERMIT_HR)
    _set_mode("enforce")
    _flags(monkeypatch, interceptor=True)
    _attached(None)
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        spec = AgentSpec(**row.spec)
    finally:
        db.close()
    ctx = SimpleNamespace(scratch={}, workspace=workspace, session=SessionLocal,
                          log=lambda _m: None)
    policy = policy_enforcement.stage_policy(ctx, row, spec)
    _attached(True)  # the attach lands mid-job
    assert policy.carrier and not policy.interceptor_decides  # no proven attach when decided
    assert zr._principal_carrier(ctx, row, spec) is True  # the render reads the same job answer
    assert ctx.scratch["principal_carrier_decision"] == (True, False)
    calls = []
    monkeypatch.setattr(policy_enforcement, "carrier_decision",
                        lambda *a, **k: calls.append(1) or (False, False))
    assert policy_enforcement.job_carrier_decision(ctx, row, spec) == (True, False)
    assert calls == []
