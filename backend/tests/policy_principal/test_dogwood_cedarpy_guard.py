"""The cedarpy fail-closed guard (A6, lead 2026-10-27): off the engine route, a set holding an
applicable ``dialect='dogwood'`` row is an evaluation error and a DENY with reason
``policy_evaluation_error`` on the cedarpy path — the simulator (``policy_bundle.evaluate``:
ACTIVE, drafts, pinned; ``call_tool`` only since Global parity) — never a skipped row, never an
allow. Pure cedarpy: no engine binary needed."""

from __future__ import annotations

import pytest

from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.models.policy import Policy, PolicyVersion
from app.services import (
    policy_backend,
    policy_bundle,
    policy_cedar,
)
from app.services.policy_cedar import CedarEngine
from app.services.policy_entities import Principal

WS = "default"
PERMIT_MEMBERS = ('permit(principal in Launchpad::Role::"member", '
                  'action == Launchpad::Action::"call_tool", resource);')
PERMIT_TOOLS = ('permit(principal, action == Launchpad::Action::"call_tool", resource) '
                'when { resource.name like "hr-database___*" };')
# a Dogwood forbid that also PARSES as Cedar: cedarpy would evaluate it against Launchpad
# entities, never match it and silently allow — exactly the fail-open the guard closes
DOGWOOD_FORBID = ('forbid (principal, action == AgentCore::Action::"hr-database___get_employee",'
                  ' resource);')
DOGWOOD_TEMPORAL = ('forbid (principal, action == AgentCore::Action::"hr-database___get_employee",'
                    ' resource) unless temporal { formerly within 8h AgentCore::Action::'
                    '"hr-approvals___verify_identity"::response{ output.verified: true } };')
MEMBER = Principal(kind="user", id="hr-intern", label="hr-intern", role="member",
                   attrs={"username": "hr-intern", "role": "member"})
TOOL = "hr-database___get_employee"


def _row(db, pid, statement, *, dialect="cedar", status="active", draft=False, scope="workspace",
         agent_ids=()):
    db.add(Policy(id=pid, workspace_id=WS, name=pid, status=status, scope=scope,
                  agent_ids=list(agent_ids), enforcement_mode="active",
                  active_version=None if draft else 1, draft_version=1 if draft else None))
    db.add(PolicyVersion(workspace_id=WS, policy_id=pid, version=1, statement=statement,
                         dialect=dialect, schema_digest="x"))


@pytest.fixture
def db():
    policy_backend.set_backend(CedarEngine())
    session = SessionLocal()
    session.add(Agent(id="ag_a", workspace_id=WS, name="hr-a", method="zip_runtime",
                      status="active", spec={}))
    session.add(Agent(id="ag_b", workspace_id=WS, name="hr-b", method="zip_runtime",
                      status="active", spec={}))
    _row(session, "pol_members", PERMIT_MEMBERS)
    _row(session, "pol_tools", PERMIT_TOOLS)
    session.commit()
    policy_cedar.invalidate(WS)
    try:
        yield session
    finally:
        session.close()
        policy_cedar.invalidate(WS)


def _call(db, agent_id="ag_a", tool=TOOL, **kw):
    agent = db.get(Agent, agent_id)
    return policy_bundle.evaluate(
        db, WS, policy_bundle.call_tool_request(MEMBER, tool, agent_id=agent.id, channel="chat"),
        principal=MEMBER, agent=agent, tool_ids=(tool,), **kw)


def _assert_guard_denial(decision, stamp):
    assert decision.decision == "deny"
    assert decision.errors[0] == "policy_evaluation_error"
    assert stamp in decision.reasons


def test_without_dogwood_rows_cedarpy_decides_as_before(db):
    assert _call(db).outcome == "ALLOW"
    assert _call(db, tool="hr-database___create_payout").outcome == "ALLOW"


def test_an_active_dogwood_row_denies_the_tool_call_with_policy_evaluation_error(db):
    _row(db, "pol_dw", DOGWOOD_FORBID, dialect="dogwood")
    db.commit()
    policy_cedar.invalidate(WS)
    ev = _call(db)
    assert (ev.outcome, ev.reason) == ("DENY", "policy_evaluation_error")
    _assert_guard_denial(ev.decision, "pol_dw:v1")
    assert ev.policy_versions["pol_dw"] == 1


def test_an_active_temporal_dogwood_row_denies_every_tool_simulation(db):
    _row(db, "pol_dw", DOGWOOD_TEMPORAL, dialect="dogwood")
    db.commit()
    policy_cedar.invalidate(WS)
    for tool in (TOOL, "hr-database___create_payout"):
        _assert_guard_denial(_call(db, tool=tool).decision, "pol_dw:v1")


def test_draft_and_pinned_dogwood_rows_deny_the_simulation_too(db):
    _row(db, "pol_dw_draft", DOGWOOD_FORBID, dialect="dogwood", status="draft", draft=True)
    db.commit()
    policy_cedar.invalidate(WS)
    assert _call(db).outcome == "ALLOW", "a draft is not in the ACTIVE set"
    drafts = _call(db, include_drafts=True)
    _assert_guard_denial(drafts.decision, "pol_dw_draft:v1")
    assert drafts.source_set == "draft"
    pinned = _call(db, pinned={"pol_dw_draft": 1})
    _assert_guard_denial(pinned.decision, "pol_dw_draft:v1")


